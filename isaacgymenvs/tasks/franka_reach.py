
import numpy as np
import os
import torch

from isaacgym import gymutil, gymtorch, gymapi
from isaacgym.torch_utils import *
from .base.vec_task import VecTask

import random

os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
os.environ["HYDRA_FULL_ERROR"] = "1"

class FrankaReach(VecTask):


    def __init__(self, cfg, rl_device, sim_device, graphics_device_id, headless, virtual_screen_capture, force_render):
        self.cfg = cfg

        self.max_episode_length = self.cfg["env"]["episodeLength"]

        self.action_scale = self.cfg["env"]["actionScale"]
        self.start_position_noise = self.cfg["env"]["startPositionNoise"]
        self.start_rotation_noise = self.cfg["env"]["startRotationNoise"]
        # self.num_props = self.cfg["env"]["numProps"]
        self.aggregate_mode = self.cfg["env"]["aggregateMode"]

        self.dof_vel_scale = self.cfg["env"]["dofVelocityScale"]
        self.dist_reward_scale = self.cfg["env"]["distRewardScale"]
        self.rot_reward_scale = self.cfg["env"]["rotRewardScale"]
        self.open_reward_scale = self.cfg["env"]["openRewardScale"]
        self.finger_dist_reward_scale = self.cfg["env"]["fingerDistRewardScale"]
        self.action_penalty_scale = self.cfg["env"]["actionPenaltyScale"]

        self.debug_viz = self.cfg["env"]["enableDebugVis"]

        self.up_axis = "z"
        self.up_axis_idx = 2

        self.distX_offset = 0.04
        self.dt = 1 / 60.

        # prop dimensions
        self.prop_width = 0.08
        self.prop_height = 0.08
        self.prop_length = 0.08
        self.prop_spacing = 0.09

        num_obs = 23
        num_acts = 9

        self.cfg["env"]["numObservations"] = 21
        self.cfg["env"]["numActions"] = 9

        super().__init__(config=self.cfg,
                         sim_device=sim_device,
                         graphics_device_id=graphics_device_id,
                         headless=headless,
                         rl_device=rl_device,
                         )

        # get gym GPU state tensors
        actor_root_state_tensor = self.gym.acquire_actor_root_state_tensor(self.sim)
        dof_state_tensor = self.gym.acquire_dof_state_tensor(self.sim)
        rigid_body_tensor = self.gym.acquire_rigid_body_state_tensor(self.sim)

        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)

        # create some wrapper tensors for different slices
        self.franka_default_dof_pos = to_torch([1.157, -1.066, -0.155, -2.239, -1.841, 1.003, 0.469, 0.035, 0.035],
                                               device=self.device)
        self.dof_state = gymtorch.wrap_tensor(dof_state_tensor)
        self.franka_dof_state = self.dof_state.view(self.num_envs, -1, 2)[:, :self.num_franka_dofs]
        self.franka_dof_pos = self.franka_dof_state[..., 0]
        self.franka_dof_vel = self.franka_dof_state[..., 1]
        # self.cabinet_dof_state = self.dof_state.view(self.num_envs, -1, 2)[:, self.num_franka_dofs:]
        # self.cabinet_dof_pos = self.cabinet_dof_state[..., 0]
        # self.cabinet_dof_vel = self.cabinet_dof_state[..., 1]

        self.rigid_body_states = gymtorch.wrap_tensor(rigid_body_tensor).view(self.num_envs, -1, 13)
        self.num_bodies = self.rigid_body_states.shape[1]

        # self.root_state_tensor = gymtorch.wrap_tensor(actor_root_state_tensor).view(self.num_envs, -1, 13)

        self.root_state_tensor = gymtorch.wrap_tensor(actor_root_state_tensor)
        # if self.num_props > 0:
        #     self.prop_states = self.root_state_tensor[:, 2:]

        self.num_dofs = self.gym.get_sim_dof_count(self.sim) // self.num_envs
        self.franka_dof_targets = torch.zeros((self.num_envs, self.num_dofs), dtype=torch.float, device=self.device)

        self.global_indices = torch.arange(self.num_envs * (2), dtype=torch.int32,
                                           device=self.device).view(self.num_envs, -1)

        print('Reset indices: ', torch.arange(self.num_envs))

        self.franka_indices = []

        self.reset_idx(torch.arange(self.num_envs, device=self.device))


    def create_sim(self):
        self.sim_params.up_axis = gymapi.UP_AXIS_Z
        self.sim_params.gravity.x = 0
        self.sim_params.gravity.y = 0
        self.sim_params.gravity.z = -9.81
        self.sim = super().create_sim(
            self.device_id, self.graphics_device_id, self.physics_engine, self.sim_params)
        self._create_ground_plane()
        self._create_envs(self.num_envs, self.cfg["env"]['envSpacing'], int(np.sqrt(self.num_envs)))


    def _create_ground_plane(self):
        plane_params = gymapi.PlaneParams()
        plane_params.normal = gymapi.Vec3(0.0, 0.0, 1.0)
        self.gym.add_ground(self.sim, plane_params)


    def _create_envs(self, num_envs, spacing, num_per_row):
        lower = gymapi.Vec3(-spacing, -spacing, 0.0)
        upper = gymapi.Vec3(spacing, spacing, spacing)

        asset_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../assets")
        franka_asset_file = "urdf/franka_description/robots/franka_panda.urdf"
        cabinet_asset_file = "urdf/sektion_cabinet_model/urdf/box1.urdf"
        cylinder_asset_file = "urdf/sektion_cabinet_model/urdf/cylinder.urdf"

        # load franka asset
        if "asset" in self.cfg["env"]:
            asset_root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      self.cfg["env"]["asset"].get("assetRoot", asset_root))
            franka_asset_file = self.cfg["env"]["asset"].get("assetFileNameFranka", franka_asset_file)
            # cabinet_asset_file = self.cfg["env"]["asset"].get("assetFileNameCabinet", cabinet_asset_file)

        # ball_asset_options = gymapi.AssetOptions()
        # ball_asset_options.disable_gravity = True
        # ball_asset_options.fix_base_link = True
        #
        # ball_asset = self.gym.create_sphere(self.sim, 0.05, ball_asset_options)
        # ball_dof_props = self.gym.get_asset_dof_properties(ball_asset)

        # load franka asset
        asset_options = gymapi.AssetOptions()
        asset_options.flip_visual_attachments = True
        asset_options.fix_base_link = True
        asset_options.collapse_fixed_joints = True
        asset_options.disable_gravity = True
        asset_options.thickness = 0.001
        asset_options.default_dof_drive_mode = gymapi.DOF_MODE_POS
        asset_options.use_mesh_materials = True
        franka_asset = self.gym.load_asset(self.sim, asset_root, franka_asset_file, asset_options)

        franka_dof_stiffness = to_torch([400, 400, 400, 400, 400, 400, 400, 1.0e6, 1.0e6], dtype=torch.float,
                                        device=self.device)
        franka_dof_damping = to_torch([80, 80, 80, 80, 80, 80, 80, 1.0e2, 1.0e2], dtype=torch.float, device=self.device)

        self.num_franka_bodies = self.gym.get_asset_rigid_body_count(franka_asset)
        self.num_franka_dofs = self.gym.get_asset_dof_count(franka_asset)

        print("num franka bodies: ", self.num_franka_bodies)
        print("num franka dofs: ", self.num_franka_dofs)

        box_cyl_asset_options = gymapi.AssetOptions()
        box_cyl_asset_options.fix_base_link = True
        box_cyl_asset_options.flip_visual_attachments = True
        box_cyl_asset_options.armature = 0.01

        box_asset = self.gym.load_asset(self.sim, asset_root, cabinet_asset_file, box_cyl_asset_options)
        cylinder_asset = self.gym.load_asset(self.sim, asset_root, cylinder_asset_file, box_cyl_asset_options)

        # Initial box & cylinder poses
        box_pose = gymapi.Transform()
        box_pose.p = gymapi.Vec3(1.0, 0.0, 0.0)
        box_pose.r = gymapi.Quat(-0.707107, 0.0, 0.0, 0.707107)

        cylinder_pose = gymapi.Transform()
        cylinder_pose.p = gymapi.Vec3(1.0, 0.2, 0.0)
        cylinder_pose.r = gymapi.Quat(-0.707107, 0.0, 0.0, 0.707107)

        box_dof_props = self.gym.get_asset_dof_properties(box_asset)
        print("box_dof_props: ", box_dof_props)
        cylinder_dof_props = self.gym.get_asset_dof_properties(cylinder_asset)

        # set franka dof properties
        franka_dof_props = self.gym.get_asset_dof_properties(franka_asset)
        self.franka_dof_lower_limits = []
        self.franka_dof_upper_limits = []
        for i in range(self.num_franka_dofs):
            franka_dof_props['driveMode'][i] = gymapi.DOF_MODE_POS
            if self.physics_engine == gymapi.SIM_PHYSX:
                franka_dof_props['stiffness'][i] = franka_dof_stiffness[i]
                franka_dof_props['damping'][i] = franka_dof_damping[i]
            else:
                franka_dof_props['stiffness'][i] = 7000.0
                franka_dof_props['damping'][i] = 50.0

            self.franka_dof_lower_limits.append(franka_dof_props['lower'][i])
            self.franka_dof_upper_limits.append(franka_dof_props['upper'][i])

        self.franka_dof_lower_limits = to_torch(self.franka_dof_lower_limits, device=self.device)
        self.franka_dof_upper_limits = to_torch(self.franka_dof_upper_limits, device=self.device)
        self.franka_dof_speed_scales = torch.ones_like(self.franka_dof_lower_limits)
        self.franka_dof_speed_scales[[7, 8]] = 0.1
        franka_dof_props['effort'][7] = 200
        franka_dof_props['effort'][8] = 200

        franka_start_pose = gymapi.Transform()
        franka_start_pose.p = gymapi.Vec3(1.0, 0.0, 0.0)
        franka_start_pose.r = gymapi.Quat(0.0, 0.0, 1.0, 0.0)

        franka_start_tip_pose = gymapi.Transform()
        franka_start_tip_pose.p = gymapi.Vec3(1.3, 0.0, 1)

        # compute aggregate size
        num_franka_bodies = self.gym.get_asset_rigid_body_count(franka_asset)
        num_franka_shapes = self.gym.get_asset_rigid_shape_count(franka_asset)

        # num_ball_bodies = self.gym.get_asset_rigid_body_count(ball_asset)
        # num_ball_shapes = self.gym.get_asset_rigid_shape_count(ball_asset)

        num_box_bodies = self.gym.get_asset_rigid_body_count(box_asset)
        num_box_shapes = self.gym.get_asset_rigid_shape_count(box_asset)

        num_cylinder_bodies = self.gym.get_asset_rigid_body_count(cylinder_asset)
        num_cylinder_shapes = self.gym.get_asset_rigid_shape_count(cylinder_asset)

        # compute aggregate size

        max_agg_bodies = num_franka_bodies + num_box_bodies + num_cylinder_bodies # + num_ball_bodies
        max_agg_shapes = num_franka_shapes  + num_box_shapes + num_cylinder_shapes  # + num_ball_shapes

        self.frankas = []
        self.boxes = []
        self.cylinders = []
        self.envs = []
        self.franka_indices = []

        for i in range(self.num_envs):
            # create env instance
            env_ptr = self.gym.create_env(
                self.sim, lower, upper, num_per_row
            )

            if self.aggregate_mode >= 3:
                self.gym.begin_aggregate(env_ptr, max_agg_bodies, max_agg_shapes, True)

            franka_actor = self.gym.create_actor(env_ptr, franka_asset, franka_start_pose, "franka", i, 1, 0)
            self.gym.set_actor_dof_properties(env_ptr, franka_actor, franka_dof_props)

            franka_idx = self.gym.get_actor_index(env_ptr, franka_actor, gymapi.DOMAIN_SIM)
            self.franka_indices.append(franka_idx)

            # box & cylinder actors
            box_handle = self.gym.create_actor(env_ptr, box_asset, box_pose, "box")
            box_dict = self.gym.get_actor_rigid_body_dict(env_ptr, box_handle)
            self.boxes.append(box_handle)
            self.gym.set_actor_dof_properties(env_ptr, box_handle, box_dof_props)

            cylinder_handle = self.gym.create_actor(env_ptr, cylinder_asset, cylinder_pose, "cylinder")
            cylinder_dict = self.gym.get_actor_rigid_body_dict(env_ptr, cylinder_handle)
            self.cylinders.append(cylinder_handle)
            self.gym.set_actor_dof_properties(env_ptr, cylinder_handle, cylinder_dof_props)

            # ball_actor = self.gym.create_actor(env_ptr, ball_asset, franka_start_tip_pose, "ball", 2)
            # ball_actor_idx = self.gym.get_actor_index(env_ptr, ball_actor, gymapi.DOMAIN_SIM)
            # self.ball_actor_indices.append(ball_actor_idx)
            # self.gym.set_actor_dof_properties(env_ptr, ball_actor, ball_dof_props)

            if self.aggregate_mode == 2:
                self.gym.begin_aggregate(env_ptr, max_agg_bodies, max_agg_shapes, True)

            if self.aggregate_mode > 0:
                self.gym.end_aggregate(env_ptr)

            self.envs.append(env_ptr)
            self.frankas.append(franka_actor)

        self.hand_handle = self.gym.find_actor_rigid_body_handle(env_ptr, franka_actor, "panda_link7")

        print(self.franka_indices)

        # self.ball_actor_indices = to_torch(self.ball_actor_indices, dtype=torch.long, device=self.device)
        self.init_data()


    def init_data(self):
        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)

        hand = self.gym.find_actor_rigid_body_handle(self.envs[0], self.frankas[0], "panda_link7")
        hand_pose = self.gym.get_rigid_transform(self.envs[0], hand)


    def compute_reward(self, actions):
        self.rew_buf[:], self.reset_buf[:] = compute_franka_reward(
            self.reset_buf, self.progress_buf, self.hand_pos, self.sphere_poses, self.max_episode_length
        )


    def compute_observations(self):
        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)

        self.hand_pos = self.rigid_body_states[:, self.hand_handle][:, 0:3]

        # self.sphere_poses = self.root_state_tensor[self.ball_actor_indices, 0:3]

        self.box_poses = self.rigid_body_states[:, self.boxes[0]][:, 0:3]
        self.cylinder_poses = self.rigid_body_states[:, self.cylinders[0]][:, 0:3]

        dof_pos_scaled = (2.0 * (self.franka_dof_pos - self.franka_dof_lower_limits)
                          / (self.franka_dof_upper_limits - self.franka_dof_lower_limits) - 1.0)
        # to_target = self.sphere_poses - self.hand_pos

        self.obs_buf[..., 0:9] = dof_pos_scaled
        self.obs_buf[..., 9:18] = self.franka_dof_vel
        # print(self.franka_dof_vel[0])
        self.obs_buf[..., 18:21] = self.sphere_poses
        self.obs_buf[..., 21:24] = self.box_poses
        self.obs_buf[..., 24:27] = self.cylinder_poses

        return self.obs_buf


    def reset_idx(self, env_ids):
        print("Environment IDs: ", env_ids)
        env_ids_int32 = env_ids.to(dtype=torch.int32)


        # reset franka
        pos = tensor_clamp(
            self.franka_default_dof_pos.unsqueeze(0) + 0.25 * (
                    torch.rand((len(env_ids), self.num_franka_dofs), device=self.device) - 0.5),
            self.franka_dof_lower_limits, self.franka_dof_upper_limits)

        self.franka_dof_pos[env_ids, :] = pos
        self.franka_dof_vel[env_ids, :] = torch.zeros_like(self.franka_dof_vel[env_ids])
        self.franka_dof_targets[env_ids, :self.num_franka_dofs] = pos

        multi_env_ids_int32 = self.global_indices[env_ids, :1].flatten()
        print(f'Env_ids: {env_ids}')

        self.gym.set_dof_position_target_tensor_indexed(self.sim,
                                                        gymtorch.unwrap_tensor(self.franka_dof_targets),
                                                        gymtorch.unwrap_tensor(multi_env_ids_int32),
                                                        len(multi_env_ids_int32))


        print("Gym Indices: ", self.franka_indices)
        franka_indices = torch.tensor(self.franka_indices, dtype=torch.int32, device=self.device)

        self.gym.set_dof_state_tensor_indexed(self.sim,
                                              gymtorch.unwrap_tensor(self.dof_state),
                                              gymtorch.unwrap_tensor(franka_indices), len(franka_indices))

        if env_ids.device != self.progress_buf.device:
            env_ids = env_ids.to(self.progress_buf.device)

        if franka_indices.device != self.progress_buf.device:
            env_ids = env_ids.to(self.progress_buf.device)

        print(env_ids)
        self.progress_buf[env_ids] = 0
        self.reset_buf[env_ids] = 0


    def pre_physics_step(self, actions):
        self.actions = actions.clone().to(self.device)
        targets = self.franka_dof_targets[:,
                  :self.num_franka_dofs] + self.franka_dof_speed_scales * self.dt * self.actions * self.action_scale
        self.franka_dof_targets[:, :self.num_franka_dofs] = tensor_clamp(
            targets, self.franka_dof_lower_limits, self.franka_dof_upper_limits)
        env_ids_int32 = torch.arange(self.num_envs, dtype=torch.int32, device=self.device)
        self.gym.set_dof_position_target_tensor(self.sim,
                                                gymtorch.unwrap_tensor(self.franka_dof_targets))


    def post_physics_step(self):
        self.progress_buf += 1

        env_ids = self.reset_buf.nonzero(as_tuple=False).squeeze(-1)
        if len(env_ids) > 0:
            print("Resetting envs: ", env_ids)
            self.reset_idx(env_ids)

        self.compute_observations()
        self.compute_reward(self.actions)

        # debug viz
        if self.viewer and self.debug_viz:
            self.gym.clear_lines(self.viewer)
            self.gym.refresh_rigid_body_state_tensor(self.sim)


#####################################################################
###=========================jit functions=========================###
#####################################################################

@torch.jit.script
def compute_franka_reward(
        reset_buf, progress_buf, hand_pos, sphere_poses, max_episode_length
):


    # type: (Tensor, Tensor, Tensor, Tensor, float) -> Tuple[Tensor, Tensor]

    # distance from hand to the drawer
    d = torch.norm(hand_pos - sphere_poses, p=2, dim=-1)
    dist_reward = 1.0 / (1.0 + d ** 2)
    dist_reward *= dist_reward
    dist_reward = torch.where(d <= 0.02, dist_reward * 2, dist_reward)

    rewards = dist_reward

    reset_buf = torch.where(abs(d) < 0.02,
                            torch.ones_like(reset_buf), reset_buf)
    reset_buf = torch.where(progress_buf >= max_episode_length - 1, torch.ones_like(reset_buf), reset_buf)

    return rewards, reset_buf


@torch.jit.script
def compute_grasp_transforms(hand_rot, hand_pos, franka_local_grasp_rot, franka_local_grasp_pos,
                             drawer_rot, drawer_pos, drawer_local_grasp_rot, drawer_local_grasp_pos
                             ):


# type: (Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor) -> Tuple[Tensor, Tensor, Tensor, Tensor]

    global_franka_rot, global_franka_pos = tf_combine(
        hand_rot, hand_pos, franka_local_grasp_rot, franka_local_grasp_pos)
    global_drawer_rot, global_drawer_pos = tf_combine(
        drawer_rot, drawer_pos, drawer_local_grasp_rot, drawer_local_grasp_pos)

    return global_franka_rot, global_franka_pos, global_drawer_rot, global_drawer_pos