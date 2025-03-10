import numpy as np
import pybullet as p

class ScoreUtil:
    def __init__(self, pid, util):
        self.pid = pid
        self.util = util

    def init_score_util(self, humanoid_id, right_shoulder, right_elbow, human_controllable_joints,
                        robot, robot_2, tool,
                        target_closer_to_eef, robot_2_in_collision, robot_in_collision):
        self.humanoid_id = humanoid_id
        self.right_shoulder = right_shoulder
        self.right_elbow = right_elbow
        self.human_controllable_joints = human_controllable_joints

        self.robot = robot
        self.robot_2 = robot_2
        self.tool = tool

        self.target_closer_to_eef = target_closer_to_eef
        self.robot_2_in_collision = robot_2_in_collision
        self.robot_in_collision = robot_in_collision
        
        min_q_H = [-3.14, -0.24, -2.66, 0.4]
        max_q_H = [3.14, 1.23, -1.32, 2.54]
        self.max_dist = np.linalg.norm(np.array(min_q_H) - np.array(max_q_H))

    def reset(self, targets_pos_upperarm_world, targets_orn_upperarm_world, targets_pos_forearm_world, targets_orn_forearm_world, 
              q_H, q_robot):
        self.targets_pos_upperarm_world = targets_pos_upperarm_world
        self.targets_orn_upperarm_world = targets_orn_upperarm_world
        self.targets_pos_forearm_world = targets_pos_forearm_world
        self.targets_orn_forearm_world = targets_orn_forearm_world

        self.total_targets = len(targets_pos_upperarm_world) + len(targets_pos_forearm_world)
        self.q_H = q_H
        self.q_robot = q_robot

    def compute_score_by_feasibility(self):
        # reset robot & human joint states
        for i, joint_id in enumerate(self.robot.arm_controllable_joints):
            p.resetJointState(self.robot.id, joint_id, self.q_robot[i], physicsClientId=self.pid)
        for i, j in enumerate(self.human_controllable_joints):
            p.resetJointState(self.humanoid_id, j, self.q_H[i], physicsClientId=self.pid)

        # check upperarm targets
        reachable_targets_count = 0
        for target_pos, target_orn in zip(self.targets_pos_upperarm_world, self.targets_orn_upperarm_world):
            world_to_eef = p.multiplyTransforms(target_pos, target_orn,
                                                self.target_closer_to_eef[0], self.target_closer_to_eef[1], physicsClientId=self.pid)
            q_robot_2_closer = p.calculateInverseKinematics(self.robot_2.id, self.robot_2.eef_id, world_to_eef[0], world_to_eef[1],
                                                            lowerLimits=self.robot_2.arm_lower_limits, upperLimits=self.robot_2.arm_upper_limits, 
                                                            jointRanges=self.robot_2.arm_joint_ranges, restPoses=self.robot_2.arm_rest_poses,
                                                            maxNumIterations=40, physicsClientId=self.pid)
            q_robot_2_closer = [q_robot_2_closer[i] for i in range(len(self.robot_2.arm_controllable_joints))]
            if min(q_robot_2_closer) < min(self.robot_2.arm_lower_limits) or max(q_robot_2_closer) > max(self.robot_2.arm_upper_limits):  # invalid joint state
                continue

            for i, joint_id in enumerate(self.robot_2.arm_controllable_joints):
                p.resetJointState(self.robot_2.id, joint_id, q_robot_2_closer[i], physicsClientId=self.pid)

            # check if config is valid
            eef_pose = p.getLinkState(self.robot_2.id, self.robot_2.eef_id, computeForwardKinematics=True, physicsClientId=self.pid)[:2]
            pos_dist = np.linalg.norm(np.array(world_to_eef[0]) - np.array(eef_pose[0]))
            dot_product = np.abs(np.dot(world_to_eef[1], eef_pose[1]))
            orn_dist = 2 * np.arccos(np.clip(dot_product, -1.0, 1.0))
            if self.robot_2_in_collision(q_robot_2_closer) or pos_dist > 0.02 or orn_dist > np.deg2rad(15):
                continue

            # target is reachable
            reachable_targets_count += 1

        # check forearm targets
        for target_pos, target_orn in zip(self.targets_pos_forearm_world, self.targets_orn_forearm_world):
            world_to_eef = p.multiplyTransforms(target_pos, target_orn,
                                                self.target_closer_to_eef[0], self.target_closer_to_eef[1], physicsClientId=self.pid)
            q_robot_2_closer = p.calculateInverseKinematics(self.robot_2.id, self.robot_2.eef_id, world_to_eef[0], world_to_eef[1],
                                                            lowerLimits=self.robot_2.arm_lower_limits, upperLimits=self.robot_2.arm_upper_limits, 
                                                            jointRanges=self.robot_2.arm_joint_ranges, restPoses=self.robot_2.arm_rest_poses,
                                                            maxNumIterations=40, physicsClientId=self.pid)
            q_robot_2_closer = [q_robot_2_closer[i] for i in range(len(self.robot_2.arm_controllable_joints))]
            if min(q_robot_2_closer) < min(self.robot_2.arm_lower_limits) or max(q_robot_2_closer) > max(self.robot_2.arm_upper_limits):  # invalid joint state
                continue

            for i, joint_id in enumerate(self.robot_2.arm_controllable_joints):
                p.resetJointState(self.robot_2.id, joint_id, q_robot_2_closer[i], physicsClientId=self.pid)

            # check if config is valid
            eef_pose = p.getLinkState(self.robot_2.id, self.robot_2.eef_id, computeForwardKinematics=True, physicsClientId=self.pid)[:2]
            pos_dist = np.linalg.norm(np.array(world_to_eef[0]) - np.array(eef_pose[0]))
            dot_product = np.abs(np.dot(world_to_eef[1], eef_pose[1]))
            orn_dist = 2 * np.arccos(np.clip(dot_product, -1.0, 1.0))
            if self.robot_2_in_collision(q_robot_2_closer) or pos_dist > 0.02 or orn_dist > np.deg2rad(15):
                continue

            # target is reachable
            reachable_targets_count += 1

        score = reachable_targets_count/self.total_targets

        return score
    
    def compute_score_by_closeness(self, q_H_init, q_H_goal):
        dist = np.linalg.norm(np.array(q_H_init) - np.array(q_H_goal))
        score = (self.max_dist-dist)/self.max_dist
        return score