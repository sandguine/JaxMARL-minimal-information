import jax 
import jax.numpy as jnp
import chex
from typing import Tuple, Dict
from functools import partial
from smax.environments.mpe.simple import SimpleMPE, TargetState, EnvParams
from smax.environments.mpe.default_params import *
from gymnax.environments.spaces import Box

# Obstacle Colours
COLOUR_1 = jnp.array([0.1, 0.9, 0.1])
COLOUR_2 = jnp.array([0.1, 0.1, 0.9])
OBS_COLOUR = jnp.concatenate([COLOUR_1, COLOUR_2])

class SimplePushMPE(SimpleMPE):

    def __init__(self,
                 num_good_agents=1,
                 num_adversaries=1,
                 num_landmarks=2,):
        
        assert num_landmarks == 2, "SimplePushMPE only supports 2 landmarks (yes, this is a departure from the docs but follows the code)" 
        
        dim_c = 2 # NOTE follows code rather than docs

        num_agents = num_good_agents + num_adversaries
        num_landmarks = num_landmarks 

        self.num_good_agents, self.num_adversaries = num_good_agents, num_adversaries

        self.adversaries = ["adversary_{}".format(i) for i in range(num_adversaries)]
        self.good_agents = ["agent_{}".format(i) for i in range(num_good_agents)]
        agents = self.adversaries + self.good_agents

        landmarks = ["landmark {}".format(i) for i in range(num_landmarks)]

        # Action and observation spaces
        action_spaces = {i: Box(0.0, 1.0, (5,)) for i in agents}

        observation_spaces = {i: Box(-jnp.inf, jnp.inf, (8,)) for i in self.adversaries }
        observation_spaces.update({i: Box(-jnp.inf, jnp.inf, (19,)) for i in self.good_agents})

        colour = [ADVERSARY_COLOUR] * num_adversaries + [AGENT_COLOUR] * num_good_agents + \
            list(OBS_COLOUR)
        
        super().__init__(num_agents=num_agents, 
                         agents=agents,
                         num_landmarks=num_landmarks,
                         landmarks=landmarks,
                         action_spaces=action_spaces,
                         observation_spaces=observation_spaces,
                         dim_c=dim_c,
                         colour=colour)
        
    @property
    def default_params(self) -> EnvParams:
        params = EnvParams(
            max_steps=MAX_STEPS,
            rad=jnp.concatenate([jnp.full((self.num_agents), AGENT_RADIUS),
                            jnp.full((self.num_landmarks), LANDMARK_RADIUS)]),
            moveable=jnp.concatenate([jnp.full((self.num_agents), True), jnp.full((self.num_landmarks), False)]),
            silent = jnp.full((self.num_agents), 1),
            collide = jnp.concatenate([jnp.full((self.num_agents), True), jnp.full((self.num_landmarks), False)]),
            mass=jnp.full((self.num_entities), MASS),
            accel = jnp.full((self.num_agents), ACCEL),
            max_speed = jnp.concatenate([jnp.full((self.num_agents), MAX_SPEED),
                                jnp.full((self.num_landmarks), 0.0)]),
            u_noise=jnp.full((self.num_agents), 0),
            c_noise=jnp.full((self.num_agents), 0),
            damping=DAMPING,  # physical damping
            contact_force=CONTACT_FORCE,  # contact response parameters
            contact_margin=CONTACT_MARGIN,
            dt=DT,       
        )
        return params
    
    def reset_env(self, key: chex.PRNGKey, params: EnvParams) -> Tuple[chex.Array, TargetState]:
        
        key_a, key_l, key_g = jax.random.split(key, 3)        
        
        p_pos = jnp.concatenate([
            jax.random.uniform(key_a, (self.num_agents, 2), minval=-1, maxval=+1),
            jax.random.uniform(key_l, (self.num_landmarks, 2), minval=-0.9, maxval=+0.9)
        ])
        
        g_idx = jax.random.randint(key_g, (), minval=0, maxval=self.num_landmarks)
        
        state = TargetState(
            p_pos=p_pos,
            p_vel=jnp.zeros((self.num_entities, self.dim_p)),
            c=jnp.zeros((self.num_agents, self.dim_c)),
            done=jnp.full((self.num_agents), False),
            step=0,
            goal=g_idx,
        )
        
        return self.get_obs(state, params), state

    def get_obs(self, state: TargetState, params: EnvParams):

        @partial(jax.vmap, in_axes=(0, None, None))
        def _common_stats(aidx, state, params):
            """ Values needed in all observations """
            
            landmark_pos = state.p_pos[self.num_agents:] - state.p_pos[aidx]  # Landmark positions in agent reference frame

            # Zero out unseen agents with other_mask
            other_pos = (state.p_pos[:self.num_agents] - state.p_pos[aidx]) 
            other_vel = state.p_vel[:self.num_agents] 
            
            # use jnp.roll to remove ego agent from other_pos and other_vel arrays
            other_pos = jnp.roll(other_pos, shift=self.num_agents-aidx-1, axis=0)[:self.num_agents-1]
            other_vel = jnp.roll(other_vel, shift=self.num_agents-aidx-1, axis=0)[:self.num_agents-1]
            
            other_pos = jnp.roll(other_pos, shift=aidx, axis=0)
            other_vel = jnp.roll(other_vel, shift=aidx, axis=0)
            
            return landmark_pos, other_pos, other_vel

        landmark_pos, other_pos, other_vel = _common_stats(self.agent_range, state, params)

        def _good(aidx):
            goal_rel_pos = state.p_pos[state.goal+self.num_agents] - state.p_pos[aidx]

            agent_colour = jnp.full((3,), 0.25)
            agent_colour = agent_colour.at[state.goal+1].set(0.75)

            return jnp.concatenate([ # TODO 
                state.p_vel[aidx].flatten(), # 2
                goal_rel_pos.flatten(), # 2
                agent_colour,
                landmark_pos[aidx].flatten(), # 5, 2
                OBS_COLOUR.flatten(), 
                other_pos[aidx].flatten(), # 5, 2
                #other_vel[aidx,-1:].flatten(), # 2
            ])


        def _adversary(aidx):
            return jnp.concatenate([
                state.p_vel[aidx].flatten(), # 2
                landmark_pos[aidx].flatten(), # 5, 2
                other_pos[aidx].flatten(), # 5, 2
            ])
        
        obs = {a: _adversary(i) for i, a in enumerate(self.adversaries)}
        obs.update({a: _good(i+self.num_adversaries) for i, a in enumerate(self.good_agents)})
        return obs
    
    def rewards(self, state: TargetState, params: EnvParams) -> Dict[str, float]:

        def _good(aidx):
            return -jnp.linalg.norm(state.p_pos[state.goal+self.num_agents] - state.p_pos[aidx])
        
        def _adversary(aidx):
            agent_dist = state.p_pos[state.goal+self.num_agents] - state.p_pos[self.num_adversaries:self.num_agents]
            pos_rew = jnp.min(jnp.linalg.norm(agent_dist, axis=1))
            neg_rew = jnp.linalg.norm(state.p_pos[state.goal+self.num_agents] - state.p_pos[aidx])
            return pos_rew - neg_rew

        rew = {a: _adversary(i) for i, a in enumerate(self.adversaries)}
        rew.update({a: _good(i+self.num_adversaries) for i, a in enumerate(self.good_agents)})
        return rew
