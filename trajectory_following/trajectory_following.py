"""
Copyright (c) 2023 Samsung Electronics Co., Ltd.

Author(s):
Vasileios Vasilopoulos (vasileios.v@samsung.com; vasilis.vasilop@gmail.com)

Licensed under the Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0) License, (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://creativecommons.org/licenses/by-nc/4.0/
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and limitations under the License.
For conditions of distribution and use, see the accompanying LICENSE file.

Modified by: Yubin Koh (koh22@purdue.edu)
"""

# General imports
import time
import os
import numpy as np
import logging
from typing import List
import open3d as o3d
import json

# Torch imports
import torch
import torch.nn as nn
import torch.nn.functional as Functional

# Import C-SDF
from csdf.pointcloud_sdf import PointCloud_CSDF

# Import pytorch-kinematics functionality
import pytorch_kinematics as pk

# Import constraint projection
from trajectory_following.trajectory_following_handshake import TrajectoryFollower_H_Handshake


class TrajectoryFollower(nn.Module):
    def __init__(
        self,
        world_to_robot_base,
        joint_limits: List[np.ndarray],
        trajectory: np.ndarray = None,
        robot_urdf_location: str = './robot.urdf',
        control_points_json: str = None,
        link_fixed: str = 'fixed_link',
        link_ee: str = 'ee_link',
        link_skeleton: List[str] = ['fixed_link','ee_link'],
        control_points_number: int = 70,
        device: str = 'cuda',
        float_dtype: torch.dtype = torch.float32,
        use_constraint_projection: bool = False,
    ):
        super(TrajectoryFollower, self).__init__()
        
        # Define robot's number of DOF
        self._njoints = joint_limits[0].shape[0]

        # Control points location
        self._control_points_json = control_points_json

        # Define number of control points
        self._control_points_number = control_points_number

        # Store device
        self._device = device
        self._float_dtype = float_dtype

        # Define lower and upper joint limits
        self._joint_limits_lower = torch.from_numpy(joint_limits[0]).to(self._device, dtype=self._float_dtype)
        self._joint_limits_upper = torch.from_numpy(joint_limits[1]).to(self._device, dtype=self._float_dtype)
        
        # Register fixed link, end effector link and link skeleton
        self._link_fixed = link_fixed
        self._link_ee = link_ee
        self._link_skeleton = link_skeleton
        
        # Define a null gripper state
        self._gripper_state = torch.Tensor([0.0, 0.0]).to(self._device)
        
        # Store trajectory
        if trajectory is not None:
            self._trajectory = torch.from_numpy(trajectory).to(self._device, dtype=self._float_dtype)
        
        # Set up differentiable FK
        self.differentiable_model = pk.build_serial_chain_from_urdf(open(robot_urdf_location).read(), self._link_ee)
        self.differentiable_model = self.differentiable_model.to(dtype = self._float_dtype, device = self._device)
        
        # Set up C-SDF - Initialize with a random point cloud
        self.csdf = PointCloud_CSDF(np.random.rand(100000,3), device=self._device)
        self.csdf.eval()
        self.csdf.to(self._device)

        # Initialize consider obstacle collisions flag
        self._consider_obstacle_collisions = True

        # Initialize object control points
        self._grasped_object_nominal_control_points = None
        self._grasped_object_grasp_T_object = None

        # Initialize T_world_to_robot_base
        self._world_to_robot_base = torch.tensor(world_to_robot_base, device = self._device, dtype = self._float_dtype)

        # Initialize constraint class as None
        self.constraint = None
        self.H_handshake = None
        self.use_constraint_projection = use_constraint_projection

        # Collision threshold
        self.collision_threshold = 0.01

        try:
            if self._control_points_json is not None:
                with open(control_points_json, "rt") as json_file:
                    control_points = json.load(json_file)

                # Write control point locations in link frames as transforms
                self.control_points = dict()
                for link_name, ctrl_point_list in control_points.items():
                    self.control_points[link_name] = []
                    for ctrl_point in ctrl_point_list:
                        ctrl_pose_link_frame = torch.tensor(ctrl_point, device = self._device, dtype = self._float_dtype)
                        self.control_points[link_name].append(ctrl_pose_link_frame)
                    self.control_points[link_name] = torch.stack(self.control_points[link_name])
        except FileNotFoundError:
            print(control_points_json + " was not found")

    def _init_H_clamping(self, eef_to_cp, right_elbow_joint_to_cp,
                        robot_base_pose, human_base_pose,
                        human_arm_lower_limits, human_arm_upper_limits, human_controllable_joints,
                        human_rest_poses, robot_rest_poses, 
                        seated):
        self.H_handshake = TrajectoryFollower_H_Handshake(eef_to_cp = eef_to_cp, 
                                                          right_elbow_joint_to_cp = right_elbow_joint_to_cp,
                                                          robot_base_pose = robot_base_pose, 
                                                          human_base_pose = human_base_pose,
                                                          human_arm_lower_limits = human_arm_lower_limits, 
                                                          human_arm_upper_limits = human_arm_upper_limits,
                                                          human_controllable_joints = human_controllable_joints, 
                                                          human_rest_poses = human_rest_poses, 
                                                          robot_rest_poses = robot_rest_poses,
                                                          seated = seated)

    def set_collision_threshold(self, collision_threshold):
        self.collision_threshold = collision_threshold

    def compute_ee_pose(self, state: torch.Tensor) -> torch.Tensor:
        
        """
        Receives a robot configuration and computes the end effector pose as a tensor.
        
        :param state: Current joint configuration (N_STATE)
        :returns: End effector pose (4 x 4)
        """
        
        # Find link locations after stacking robot configuration with gripper state
        link_transformation = self.differentiable_model.forward_kinematics(state, end_only=True)

        # Find end effector pose
        ee_pose = self._world_to_robot_base @ link_transformation[0].get_matrix().to(dtype=self._float_dtype).squeeze()
        
        return ee_pose                                        
    
    def _get_skeleton_control_points(self, state: torch.Tensor) -> torch.Tensor:
        
        """
        Receives a robot configuration and returns a list of all skeleton control points on the manipulator.
        
        :param state: Current joint configuration (BATCH_SIZE x N_STATE)
        :returns: List of control points on the robot manipulator (BATCH_SIZE x CONTROL_POINTS x 3)
        """
        
        batch_size = state.shape[0]
        
        # Find link locations after stacking robot configuration with gripper state
        augmented_robot_state = torch.cat((state, torch.tile(self._gripper_state, (batch_size, 1))), dim=1)
        link_transformations = self.differentiable_model.forward_kinematics(state, end_only=False)
        
        # Initialize skeleton for control points - tensor should be BATCH_SIZE x 1 x 3
        skeleton_control_point_locations = torch.zeros((batch_size, len(self._link_skeleton), 3)).to(self._device)
        
        # Find skeleton control points
        for link_idx in range(len(self._link_skeleton)):
            skeleton_control_point_locations[:, link_idx, :] = link_transformations[self._link_skeleton[link_idx]].get_matrix()[:, :3, 3]

        # Find end effector pose 
        ee_pose = link_transformations[self._link_skeleton[-1]].get_matrix()

        # Compute grasped object control points
        if self._grasped_object_grasp_T_object is not None:
            object_pose = ee_pose[:, ] @ self._grasped_object_grasp_T_object
            object_control_points = object_pose @ torch.hstack((
                self._grasped_object_nominal_control_points,
                torch.ones((self._grasped_object_nominal_control_points.shape[0],1)).to(device = self._device)
            )).transpose(0,1)
            object_control_points = object_control_points.transpose(1,2)[:, :, :3]
            skeleton_control_point_locations = torch.cat((skeleton_control_point_locations, object_control_points), dim=1)
        
        return skeleton_control_point_locations
    
    def _get_mesh_control_points(self, state: torch.Tensor) -> torch.Tensor:
        """
        Receives a robot configuration and returns a list of all control points on the manipulator.

        :param ja_batch: Current joint configuration (BATCH_SIZE x N_STATE)
        :returns: List of control points on the robot manipulator (BATCH_SIZE x CONTROL_POINTS x 3)
        """
        batch_size = state.shape[0]
        num_control_points = sum(map(len, self.control_points.values()))

        # Find link locations after stacking robot configuration with gripper state
        augmented_robot_state = torch.cat((state, torch.tile(self._gripper_state, (batch_size, 1))), dim=1)
        link_transformations = self.differentiable_model.forward_kinematics(state, end_only=False)
        # Link transformations is a dict with keys being link names, value is BATCH x 4 x 4

        # Find end effector poses (w.r.t. robot base)
        self.ee_pose = link_transformations[self._link_skeleton[-1]].get_matrix().unsqueeze(1).to(dtype = self._float_dtype)

        # Control points tensor should be BATCH x N x 3 where N is the num of control points
        control_point_locations = torch.zeros((batch_size, num_control_points, 3)).to(device = self._device)
        idx=0
        for link_name, ctrl_point_transforms in self.control_points.items():
            # find control points with base offset transform
            base_to_ctrl_points = torch.matmul(link_transformations[link_name].get_matrix().unsqueeze(1).to(device = self._device, dtype = self._float_dtype), ctrl_point_transforms)
            world_to_ctrl_points = torch.matmul(self._world_to_robot_base, base_to_ctrl_points)

            control_point_locations[:, idx : idx + ctrl_point_transforms.shape[0], :] = world_to_ctrl_points[:,:,:3,3]
            idx += ctrl_point_transforms.shape[0]

        # Compute grasped object control points
        if self._grasped_object_grasp_T_object is not None:
            T_world_to_eef = torch.matmul(self._world_to_robot_base, self.ee_pose[:, ])
            T_eef_to_obj_ctrl_pts = torch.matmul(self._grasped_object_grasp_T_object, self._grasped_object_nominal_control_points)
            T_world_to_obj_ctrl_pts = torch.matmul(T_world_to_eef, T_eef_to_obj_ctrl_pts)
            object_control_points = T_world_to_obj_ctrl_pts[:, :, :3, 3]
            control_point_locations = torch.cat((control_point_locations, object_control_points), dim=1)
        
        return control_point_locations
    
    def _get_control_points(self, state: torch.Tensor) -> torch.Tensor:
        
        """
        Receives a robot configuration and returns a list of all control points on the manipulator.
        
        :param state: Current joint configuration (BATCH_SIZE x N_STATE)
        :returns: List of control points on the robot manipulator (BATCH_SIZE x CONTROL_POINTS x 3)
        """
        
        if self._control_points_json is not None:
            # Get control points sampled from the robot's mesh
            control_point_locations = self._get_mesh_control_points(state)

            # In this case, skeleton control points are the same
            skeleton_control_point_locations = control_point_locations
        else:
            # Find skeleton control points
            skeleton_control_point_locations = self._get_skeleton_control_points(state)
            
            # Augment control points based on the skeleton
            control_point_locations = Functional.interpolate(skeleton_control_point_locations.transpose(1,2), size=self._control_points_number, mode='linear', align_corners=True).transpose(1,2)
        
        return skeleton_control_point_locations, control_point_locations
    
    def update_trajectory(
        self,
        trajectory: np.ndarray,
        consider_obstacle_collisions: bool = True,
    ):

        """
        Update the trajectory to follow.
        
        :param trajectory: Trajectory (N x 3)
        :param consider_obstacle_collisions: Flag to consider or ignore obstacle collisions
        """

        # Update trajectory
        trajectory = torch.from_numpy(trajectory).unsqueeze(0).to(self._device, dtype=self._float_dtype)

        # Interpolate trajectory
        interpolated_trajectory = Functional.interpolate(trajectory.transpose(1,2), size=500, mode='linear', align_corners=True).transpose(1,2)
        self._trajectory = interpolated_trajectory[0]

        # Compute the skeleton control point locations for all configurations in the trajectory
        if self._control_points_json is not None:
            self._trajectory_skeleton_control_points = self._get_mesh_control_points(self._trajectory)
        else:
            self._trajectory_skeleton_control_points = self._get_skeleton_control_points(self._trajectory)

        # Compute distances of skeleton control points to scene point cloud
        self._trajectory_skeleton_control_points_distances = self.csdf.compute_distances(self._trajectory_skeleton_control_points)

        # Consider obstacle collisions or not
        self._consider_obstacle_collisions = consider_obstacle_collisions
    
    def attractive_potential(
        self,
        state: torch.Tensor,
        skeleton_control_points: torch.Tensor,
        sdf_value: torch.Tensor,
    ) -> torch.Tensor:
        
        """
        Compute the attractive potential.
        
        :param state: Joint configurations (BATCH_SIZE x N_STATE)
        :param skeleton_control_points: Skeleton control points (BATCH_SIZE x CONTROL_POINTS x 3)
        :param sdf_value: SDF value for each configuration (BATCH_SIZE)
        :returns: Attractive potential to the trajectory (BATCH_SIZE)
        """

        # Find the trajectory indices that lie within the given SDF value
        distance_diff = self._trajectory_skeleton_control_points - skeleton_control_points
        distance_diff_norm = torch.linalg.norm(distance_diff, dim=2)
        if self._consider_obstacle_collisions:
            valid_waypoints = torch.all(distance_diff_norm <= sdf_value+0.05, dim=1)
        else:
            valid_waypoints = torch.all(distance_diff_norm <= 0.1, dim=1)

        # Pick as goal the furthest valid waypoint
        if len(self._trajectory[valid_waypoints]) > 0:
            goal = self._trajectory[valid_waypoints][-1]
        else:
            goal = state
        
        # Attractive potential is just the distance to this goal
        dist = torch.linalg.norm(state - goal, dim=-1)
        
        return dist**2
    
    def implicit_obstacles(
        self,
        control_points: torch.Tensor,
        eef_matrix: torch.Tensor,
        # collision_threshold: float = 0.03,
    ) -> torch.Tensor:
        
        """
        Compute the repulsive potential.
        
        :param control_points: Control points (BATCH_SIZE x CONTROL_POINTS x 3)
        :param collision_threshold: Threshold below which a configuration is considered to be in collision
        :returns: SDF values for each configuration (BATCH_SIZE)
        """
        
        # Evaluate C-SDF based on these points
        sdf_values = self.csdf.forward(control_points) - self.collision_threshold

        return sdf_values, sdf_values
    
    def update_obstacle_pcd(
        self,
        pcd: np.ndarray,
    ):

        """
        Update the total point cloud used for obstacle avoidance.
        
        :param pcd: Point cloud (N x 3)
        """

        # Update point cloud in SDF
        self.csdf.update_pcd(pcd)

    def attach_to_gripper(
        self,
        object_geometry,
        object_type,
        T_eef_to_obj,
        T_obj_to_world,
        T_world_to_human_base=None,
        T_right_elbow_joint_to_cp=None,
        human_arm_lower_limits=None,
        human_arm_upper_limits=None,
    ) -> bool:

        """
        Attach object to gripper and consider the whole arm+object pair for planning.
        
        :param object_geometry: Object geometry (path to desired file for "mesh", or num_points x 6 numpy.ndarray for "pcd")
        :param world_T_grasp: Grasp pose of the gripper
        :param object_name: Name of the object to be updated
        :param object_type: Type of the object to be updated ("mesh" or "pcd")
        :param world_T_object: Pose of the object in world frame (not needed for pointclouds)
        :returns: True if object is successfully attached, False otherwise
        """

        # Add control points to collision checker
        if object_type == "pcd":
            # Construct tensor describing grasp_T_object (T_eef_to_obj)
            self._grasped_object_grasp_T_object = torch.from_numpy(T_eef_to_obj).to(self._device, dtype=self._float_dtype)

            # Write point clouds (in world frame) as transforms
            object_pcd_tensor = torch.tensor(object_geometry, device=self._device)
            object_pcd_num = object_pcd_tensor.shape[0]
            object_transforms = torch.zeros((object_pcd_num, 4, 4), device=self._device)
            identity_rotation = torch.eye(3, device=self._device)

            for i in range(object_pcd_num):
                object_transforms[i, :3, :3] = identity_rotation
                object_transforms[i, :3, 3] = object_pcd_tensor[i]
                object_transforms[i, 3, 3] = 1.0

            object_transforms.to(dtype=self._float_dtype)

            # Compute grasped object control points in eef frame
            T_obj_to_world = torch.from_numpy(T_obj_to_world).to(device=self._device, dtype=self._float_dtype)
            self._grasped_object_nominal_control_points = torch.matmul(T_obj_to_world, object_transforms).to(device=self._device, dtype=self._float_dtype)

        # Update trajectory control points
        if self._control_points_json is not None:
            self._trajectory_skeleton_control_points = self._get_mesh_control_points(self._trajectory)

        else:
            self._trajectory_skeleton_control_points = self._get_skeleton_control_points(self._trajectory)
        self._trajectory_skeleton_control_points_distances = self.csdf.compute_distances(self._trajectory_skeleton_control_points)

        return True
    
    def detach_from_gripper(
        self,
        object_name: str,
        to: np.ndarray = None
    ) -> bool:

        """
        Detach object from gripper and no longer consider the whole arm+object pair for planning.
        
        :param object_name: Name of the object to be detached from the gripper
        :param to: Detach object to a desired pose in the world frame
        :returns: True if object is successfully detached, False otherwise
        """

        # Detach mesh object from collision checker
        self._grasped_object_nominal_control_points = None
        self._grasped_object_grasp_T_object = None

        if self._control_points_json is not None:
            self._trajectory_skeleton_control_points = self._get_mesh_control_points(self._trajectory)
        else:
            self._trajectory_skeleton_control_points = self._get_skeleton_control_points(self._trajectory)
        self._trajectory_skeleton_control_points_distances = self.csdf.compute_distances(self._trajectory_skeleton_control_points)

        return True
    
    def forward(
        self, 
        current_ja: torch.Tensor,
    ) -> torch.Tensor:
        
        """
        Given the current joint configuration, compute the value of the potential field that tracks the trajectory while avoiding obstacles.
        
        :param current_ja: The start joint configuration
        :returns: The value of the potential field at this particular configuration
        """

        # Compute control point locations through FK for given state
        since = time.time()
        skeleton_control_points, control_points = self._get_control_points(current_ja)
        # log.info(f"Control points computed in: {time.time()-since}")

        # Compute SDF value
        since = time.time()
        eef_m = self.compute_ee_pose(current_ja)
        sdf_value, repulsive_potential = self.implicit_obstacles(control_points, eef_m)
        # log.info(f"SDF computed in: {time.time()-since}")

        since = time.time()
        attractive_potential = self.attractive_potential(current_ja, skeleton_control_points, sdf_value)
        # log.info(f"Attractive potential computed in: {time.time()-since}")
        
        # Define potential field value
        if self._consider_obstacle_collisions:
            # Main potential field definition
            potential = torch.div(1.0 + 10.0 * attractive_potential, 1.0 + 2.0 * repulsive_potential)
        else:
            # Here the robot does not care about obstacles
            potential = 10.0 * attractive_potential

        return potential

    def follow_trajectory(self, current_joint_angles, current_human_joint_angles, time_step=0.5):
        """
        Main method for trajectory following.

        :param current_joint_angles: Current robot configuration
        :param current_human_joint_angles: Current human configuration
        :returns: Computed target joint angles for POSITION_CONTROL in pybullet
        """

        # Define maximum joint speed and control gain
        MAX_SPEED = 0.3
        CONTROL_GAIN = 0.3

        # Extract current joint states
        current_joint_angles_tensor = torch.tensor(current_joint_angles).unsqueeze(0).to('cuda')
        current_joint_angles_tensor.requires_grad = True
        current_human_joint_angles = torch.tensor(current_human_joint_angles).unsqueeze(0).to('cuda')
        
        # Find the value of the potential field
        potential_field = self.forward(current_joint_angles_tensor)
        
        # Compute the gradient
        potential_field.backward()
        potential_field_grad = current_joint_angles_tensor.grad
        
        # Compute the change in joint positions (similar to velocity command, but we integrate it into position)
        delta_joint_angles = -CONTROL_GAIN * potential_field_grad * time_step
        if torch.linalg.norm(delta_joint_angles) > MAX_SPEED:
            delta_joint_angles = MAX_SPEED * delta_joint_angles / torch.linalg.norm(delta_joint_angles)
            delta_joint_angles = delta_joint_angles.to(dtype=self._float_dtype)

        # # Check for constraint if needed
        # if self.constraint is not None:
        #     # Calculate: delta_joint_angles -> desired q_R -> desired eef pose
        #     desired_joint_angles = torch.tensor(current_joint_angles, device=self._device) + delta_joint_angles  # Position update
        #     desired_joint_angles = desired_joint_angles.squeeze()
        #     desired_eef = self.compute_ee_pose(desired_joint_angles)
        #     desired_cp = (desired_eef @ self.T_eef_to_cp).squeeze(0)

        #     # Compute the jacobian of the constraint g_R
        #     J_gR = self.constraint.compute_constraint_jacobian_on_robot(current_joint_angles, desired_eef, current_human_joint_angles)
        #     J_gR = torch.tensor(J_gR).to(device=self._device, dtype=self._float_dtype)

        #     # Projection matrix of J_gR
        #     J_gR = J_gR.reshape(1, -1)
        #     pseudo_inverse_gR = torch.linalg.pinv(J_gR @ J_gR.T).to(device=self._device, dtype=self._float_dtype)
        #     I = torch.eye(J_gR.shape[1]).to(device=self._device, dtype=self._float_dtype)
        #     projection_matrix_gR = I - J_gR.T @ pseudo_inverse_gR @ J_gR

        #     # Project the joint angles delta onto the null space of the constraint
        #     delta_joint_angles_projected = (torch.mm(projection_matrix_gR, delta_joint_angles.T)).T
        #     delta_joint_angles_projected = delta_joint_angles_projected.cpu().numpy()

        #     # Compute the target joint angles (current + corrected delta)
        #     target_joint_angles = (current_joint_angles + delta_joint_angles_projected).squeeze()

        if self.H_handshake is not None:
            delta_joint_angles = delta_joint_angles.cpu().numpy()
            target_joint_angles = (current_joint_angles + delta_joint_angles).squeeze()
            target_joint_angles = self.H_handshake.clamp_human_joints(target_joint_angles)

        else:
            # If no constraint, compute the target joint angles directly
            delta_joint_angles = delta_joint_angles.cpu().numpy()
            target_joint_angles = (current_joint_angles + delta_joint_angles).squeeze()

        assert not np.isnan(target_joint_angles).any()

        return target_joint_angles


def planner_test():
    # Candidate poses
    pose1 = np.array([-0.47332507,  1.13872886,  1.30867887, -2.30050802,  2.07975602,  2.64635682, -2.65230727])
    pose2 = np.array([ 2.09151435, -0.54573566, -0.99544001, -2.25478268,  2.02075601,  2.74072695, -1.75231826])
    pose3 = np.array([ 2.17793441, -0.48076588, -0.856754,   -1.67240107,  0.553756,    2.79897308, -0.10493574])
    pose4 = np.array([ 0.45744711,  0.70788223,  0.71865666, -0.27235043,  0.553756,    2.09835196, -0.01765767])
    pose5 = np.array([ 1.52491331, -0.45537129, -0.08102775, -1.83516145,  0.553756,    2.91463614,  0.20733792])

    # Test planning time
    start_time = time.time()
    planner = TrajectoryFollower()
    print("planning time : ", time.time()-start_time)


def main():
    planner_test()

if __name__ == '__main__':
    main()
