"""
Implementation of Independent PPO (IPPO) for multi-agent environments.
Based on PureJaxRL's PPO implementation but adapted for multi-agent scenarios.
"""

# Core imports for JAX machine learning
import jax
import jax.numpy as jnp
import flax.linen as nn
import numpy as np
import optax
from flax.linen.initializers import constant, orthogonal
from typing import Sequence, NamedTuple, Any
from flax.training.train_state import TrainState
import distrax

# Environment and visualization imports
from gymnax.wrappers.purerl import LogWrapper, FlattenObservationWrapper
import jaxmarl
from jaxmarl.wrappers.baselines import LogWrapper
from jaxmarl.environments.overcooked import overcooked_layouts
from jaxmarl.viz.overcooked_visualizer import OvercookedVisualizer

# Configuration and logging imports
import hydra
from omegaconf import OmegaConf
import wandb

import matplotlib.pyplot as plt

class ActorCritic(nn.Module):
    """Neural network architecture implementing both policy (actor) and value function (critic)

    Attributes:
        action_dim: Dimension of action space
        activation: Activation function to use (either "relu" or "tanh")
    """
    action_dim: Sequence[int]  # Dimension of action space
    activation: str = "tanh"   # Activation function to use

    def setup(self):
        """Initialize layers and activation function.
        This runs once when the model is created.
        """
        # Store activation function
        self.act_fn = nn.relu if self.activation == "relu" else nn.tanh

        # Initialize dense layers with consistent naming
        self.actor_dense1 = nn.Dense(
            64, 
            kernel_init=orthogonal(np.sqrt(2)), 
            bias_init=constant(0.0),
            name="actor_dense1"
        )
        self.actor_dense2 = nn.Dense(
            64,
            kernel_init=orthogonal(np.sqrt(2)),
            bias_init=constant(0.0),
            name="actor_dense2"
        )
        self.actor_out = nn.Dense(
            self.action_dim,
            kernel_init=orthogonal(0.01),
            bias_init=constant(0.0),
            name="actor_out"
        )

        # Critic network layers
        self.critic_dense1 = nn.Dense(
            64,
            kernel_init=orthogonal(np.sqrt(2)),
            bias_init=constant(0.0),
            name="critic_dense1"
        )
        self.critic_dense2 = nn.Dense(
            64,
            kernel_init=orthogonal(np.sqrt(2)),
            bias_init=constant(0.0),
            name="critic_dense2"
        )
        self.critic_out = nn.Dense(
            1,
            kernel_init=orthogonal(1.0),
            bias_init=constant(0.0),
            name="critic_out"
        )

    @nn.compact
    def __call__(self, x):
        """Forward pass of the network.
        
        Args:
            x: Input tensor with shape (batch_size, input_dim)
               where input_dim is either base_obs_dim or base_obs_dim + action_dim
               
        Returns:
            Tuple of (action distribution, value estimate)
        """
        # Print debug information about input shape
        print("Network input x shape:", x.shape)
        print("ActorCritic input shape:", x.shape)
        
        # Expected input dimension is the last dimension of the input tensor
        expected_dim = x.shape[-1] if len(x.shape) > 1 else x.shape[0]
        print(f"Expected input dim: {expected_dim}")

        # Actor network
        actor = self.actor_dense1(x)
        actor = self.act_fn(actor)
        actor = self.actor_dense2(actor)
        actor = self.act_fn(actor)
        actor = self.actor_out(actor)
        pi = distrax.Categorical(logits=actor)

        # Critic network
        critic = self.critic_dense1(x)
        critic = self.act_fn(critic)
        critic = self.critic_dense2(critic)
        critic = self.act_fn(critic)
        critic = self.critic_out(critic)

        return pi, jnp.squeeze(critic, axis=-1)
    
class Transition(NamedTuple):
    """Container for storing experience transitions

    Attributes:
        done: Episode termination flag
        action: Actual action taken by the agent
        value: Value function estimate
        reward: Reward received
        log_prob: Log probability of action
        obs: Observation
    """
    done: jnp.ndarray      # Episode termination flag
    action: jnp.ndarray    # Action taken
    value: jnp.ndarray     # Value function estimate
    reward: jnp.ndarray    # Reward received
    log_prob: jnp.ndarray  # Log probability of action
    obs: jnp.ndarray       # Observation

def get_rollout(train_state, config):
    """Generate a single episode rollout for visualization.
    
    Runs a single episode in the environment using the current policy networks to generate
    actions. Used for visualizing agent behavior during training.
    
    Args:
        train_state: Current training state containing network parameters
        config: Dictionary containing environment and training configuration
        
    Returns:
        Episode trajectory data including states, rewards, and shaped rewards
    """
    if "DIMS" not in config:
        raise ValueError("Config is missing DIMS dictionary. Check that dimensions were properly initialized in main()")
    
    # Unpack dimensions from config at the start of the function
    dims = config["DIMS"]
    
    print("\nRollout Dimensions:")
    print(f"Base observation shape: {dims['base_obs_shape']} -> {dims['base_obs_dim']}")
    print(f"Action dimension: {dims['action_dim']}")
    print(f"Augmented observation dim: {dims['augmented_obs_dim']}\n")

    # Initialize environment
    env = jaxmarl.make(config["ENV_NAME"], **config["ENV_KWARGS"])
    # env_params = env.default_params
    # env = LogWrapper(env)

    # Verify dimensions match configuration
    assert np.prod(env.observation_space().shape) == dims["base_obs_dim"], \
        "Observation dimension mismatch in rollout"
    assert env.action_space().n == dims["action_dim"], \
        "Action dimension mismatch in rollout"

    # Initialize network
    network = ActorCritic(
        action_dim=env_dims["action_dim"], 
        activation=config["ACTIVATION"]
    )

    # Initialize seeds
    key = jax.random.PRNGKey(0)
    key, key_r, key_a = jax.random.split(key, 3)

    # Initialize observation
    init_x = jnp.zeros(dims["augmented_obs_dim"])
    init_x = init_x.flatten()
    print("Augmented init_x shape:", init_x.shape)

    network.init(key_a, init_x)
    network_params = train_state.params

    done = False

    # Reset environment and initialize tracking lists
    obs, state = env.reset(key_r)
    state_seq = [state]
    rewards = []
    shaped_rewards = []
    
    # Run episode until completion
    while not done:
        key, key_a0, key_a1, key_s = jax.random.split(key, 4)

        # obs_batch = batchify(obs, env.agents, config["NUM_ACTORS"])
        # breakpoint()

        # Flatten observations for network input
        obs = {k: v.flatten() for k, v in obs.items()}

        print("agent_0 obs shape:", obs["agent_0"].shape)
        print("agent_1 obs shape:", obs["agent_1"].shape)

        # Get actions from policy for both agents
        pi_0, _ = network.apply(network_params, obs["agent_0"])
        pi_1, _ = network.apply(network_params, obs["agent_1"])

        actions = {"agent_0": pi_0.sample(seed=key_a0), "agent_1": pi_1.sample(seed=key_a1)}
        print("actions:", actions)
        # env_act = unbatchify(action, env.agents, config["NUM_ENVS"], env.num_agents)
        # env_act = {k: v.flatten() for k, v in env_act.items()}

        # Step environment forward
        obs, state, reward, done, info = env.step(key_s, state, actions)
        print("reward:", reward)
        print("shaped reward:", info["shaped_reward"])
        done = done["__all__"]
        rewards.append(reward['agent_0'])
        shaped_rewards.append(info["shaped_reward"]['agent_0'])

        state_seq.append(state)

    # Plot rewards for visualization
    from matplotlib import pyplot as plt

    plt.plot(rewards, label="reward")
    plt.plot(shaped_rewards, label="shaped_reward")
    plt.legend()
    plt.savefig("reward.png")
    plt.show()

    return state_seq

def make_train(config):
     """Creates the main training function for IPPO with the given configuration.
    
    This function sets up the training environment, networks, and optimization process
    for training multiple agents using Independent PPO (IPPO). It handles:
    - Environment initialization and wrapping
    - Network architecture setup for both agents
    - Learning rate scheduling and reward shaping annealing
    - Training loop configuration including batch sizes and update schedules
    
    Args:
        config: Dictionary containing training hyperparameters and environment settings
               including:
               - DIMS: Environment dimensions
               - ENV_NAME: Name of environment to train in
               - ENV_KWARGS: Environment configuration parameters
               - NUM_ENVS: Number of parallel environments
               - NUM_STEPS: Number of steps per training iteration
               - TOTAL_TIMESTEPS: Total environment steps to train for
               - Learning rates, batch sizes, and other optimization parameters
               
    Returns:
        train: The main training function that takes an RNG key and executes the full
               training loop, returning the trained agent policies
    """
    # Initialize environment
    dims = config["DIMS"]
    env = jaxmarl.make(config["ENV_NAME"], **config["ENV_KWARGS"])

    # Verify dimensions match what we validated in main
    assert np.prod(env.observation_space().shape) == dims["base_obs_dim"], "Observation dimension mismatch"
    assert env.action_space().n == dims["action_dim"], "Action dimension mismatch"

    # Calculate key training parameters
    config["NUM_ACTORS"] = env.num_agents * config["NUM_ENVS"]
    config["NUM_UPDATES"] = (
        config["TOTAL_TIMESTEPS"] // config["NUM_STEPS"] // config["NUM_ENVS"] 
    )
    config["MINIBATCH_SIZE"] = (
        config["NUM_ACTORS"] * config["NUM_STEPS"] // config["NUM_MINIBATCHES"]
    )

    # Configuration printing
    print("Initializing training with config:")
    print(f"NUM_ENVS: {config['NUM_ENVS']}")
    print(f"NUM_STEPS: {config['NUM_STEPS']}")
    print(f"NUM_UPDATES: {config['NUM_UPDATES']}")
    print(f"NUM_MINIBATCHES: {config['NUM_MINIBATCHES']}")
    print(f"TOTAL_TIMESTEPS: {config['TOTAL_TIMESTEPS']}")
    print(f"ENV_NAME: {config['ENV_NAME']}")
    print(f"DIMS: {config['DIMS']}")
    
    env = LogWrapper(env, replace_info=False)
    
    # Learning rate and reward shaping annealing schedules
    # The learning rate is annealed linearly over the course of training because
    # if the learning rate is too high, the model can diverge.
    # By making the learning rate decay linearly, we can ensure that the model can converge.
    def linear_schedule(count):
        """Linear learning rate annealing schedule that decays over training.
        
        Calculates a learning rate multiplier that decreases linearly from 1.0 to 0.0
        over the course of training. Used to gradually reduce the learning rate to help
        convergence.
        
        Args:
            count: Current training step count used to calculate progress through training
        
        Returns:
            float: The current learning rate after applying the annealing schedule,
                  calculated as: base_lr * (1 - training_progress)
        """
        frac = 1.0 - (count // (config["NUM_MINIBATCHES"] * config["UPDATE_EPOCHS"])) / config["NUM_UPDATES"]
        return config["LR"] * frac
    
    # Schedule for annealing reward shaping
    rew_shaping_anneal = optax.linear_schedule(
        init_value=1.,
        end_value=0.,
        transition_steps=config["REW_SHAPING_HORIZON"]
    )

    # This is the main training loop where the training starts.
    # It initializes network with: correct number of parameters, optimizer, and learning rate annealing.
    def train(rng):
        """Main training loop for Independent PPO (IPPO) algorithm.
        
        Implements the core training loop for training multiple agents using IPPO.
        Handles network initialization, environment setup, and training iteration.
        
        Args:
            rng: JAX random number generator key for reproducibility
            
        Returns:
            Tuple containing:
            - Final trained network parameters for both agents
            - Training metrics and statistics
            - Environment states from training
            
        The training process:
        1. Initializes one policy network for parameter sharing
        2. Collects experience in parallel environments
        3. Updates policies using PPO with one shared value function
        4. Tracks and logs training metrics
        """
        # Shapes we're initializing with
        print("Action space:", env.action_space().n)
        print("Observation space shape:", env.observation_space().shape)

        # Initialize network with fixed action dimension
        network = ActorCritic(
            action_dim=dims["action_dim"],  # Use dimension from config
            activation=config["ACTIVATION"]
        )
        
        # Initialize seeds
        rng, _rng = jax.random.split(rng)

        # Initialize observation
        init_x = jnp.zeros(dims["augmented_obs_dim"])
        
        init_x = init_x.flatten()
        print("Augmented init_x shape:", init_x.shape)
        
        network_params = network.init(_rng, init_x)
        
        def create_optimizer(config):
            """Creates an optimizer chain for training each agent's neural network.
            
            The optimizer chain consists of:
            1. Gradient clipping using global norm
            2. Adam optimizer with either:
            - Annealed learning rate that decays linearly over training
            - Fixed learning rate specified in config
            
            Args:
                config: Dictionary containing optimization parameters like:
                    - ANNEAL_LR: Whether to use learning rate annealing
                    - MAX_GRAD_NORM: Maximum gradient norm for clipping
                    - LR: Base learning rate
                    
            Returns:
                optax.GradientTransformation: The composed optimizer chain
            """
            if config["ANNEAL_LR"]:
                tx = optax.chain(
                    optax.clip_by_global_norm(config["MAX_GRAD_NORM"]),  # First transformation
                    optax.adam(learning_rate=linear_schedule, eps=1e-5)  # Second transformation
                )
            else:
                tx = optax.chain(
                    optax.clip_by_global_norm(config["MAX_GRAD_NORM"]),
                    optax.adam(config["LR"], eps=1e-5)
                )
            return tx

        # Create separate optimizer chains for each agent
        tx = create_optimizer(config)

        # Create separate train states
        train_state = TrainState.create(
            apply_fn=network.apply,
            params=network_params,
            tx=tx,
        )
        
        # Initialize environment states
        rng, _rng = jax.random.split(rng)
        reset_rng = jax.random.split(_rng, config["NUM_ENVS"])
        obsv, env_state = jax.vmap(env.reset, in_axes=(0,))(reset_rng)
        
        # TRAIN LOOP
        # This function manages full update iteration including:
        # - collecting trajectories in _env_step
        # - calculating advantages in _calculate_gae
        # - updating the network in _update_epoch
        def _update_step(runner_state, unused):
            """Executes a single training update step in the IPPO algorithm.
            
            This function performs one complete update iteration including:
            1. Collecting trajectories by running the current policy in the environment
            2. Computing advantages and returns
            3. Updating the neural network using PPO
            
            Args:
                runner_state: Tuple containing:
                    - train_state: Current training state with network parameters
                    - env_state: Current environment state
                    - last_obs: Previous observations from environment
                    - update_step: Current training iteration number
                    - rng: Random number generator state
                unused: Placeholder parameter for JAX scan compatibility
                
            Returns:
                Tuple containing:
                    - Updated runner_state
                    - Metrics dictionary with training statistics
            """
            # COLLECT TRAJECTORIES
            # This function handle single environment step and collets transitions
            def _env_step(runner_state, unused):
                """Collects trajectories by running the current policy in the environment.
                
                This function performs one step of environment interaction for each agent,
                collecting trajectories for training.

                Args:
                    runner_state: Tuple containing:
                        - train_state: Current training state with network parameters
                        - env_state: Current environment state
                        - last_obs: Previous observations from environment
                        - update_step: Current training iteration number
                        - rng: Random number generator state

                    unused: Placeholder parameter for JAX scan compatibility

                    dims: Environment dimensions
                    
                Returns:
                    Tuple containing:
                        - Updated runner_state
                        - Trajectory batch, info, and processed observations
                """
                train_state, env_state, last_obs, update_step, rng = runner_state

                # SELECT ACTION
                rng, _rng = jax.random.split(rng)

                print("Initial observation shapes:")
                print(f"agent_0 obs shape: {jax.tree_map(lambda x: x.shape, last_obs['agent_0'])}")
                print(f"agent_1 obs shape: {jax.tree_map(lambda x: x.shape, last_obs['agent_1'])}")

                # First reshape both observations and add action dimensions
                agent_0_obs = last_obs['agent_0'].reshape(last_obs['agent_0'].shape[0], -1)
                agent_1_obs = last_obs['agent_1'].reshape(last_obs['agent_1'].shape[0], -1)

                # Create zero action vector for agent_1's observation
                zero_action = jnp.zeros((last_obs['agent_1'].shape[0], env.action_space().n))
                agent_1_obs_augmented = jnp.concatenate([agent_1_obs, zero_action], axis=-1)
                
                # Get agent_1's action first using the shared parameters
                pi_1, value_1 = network.apply(train_state.params, agent_1_obs_augmented)
                action_1 = pi_1.sample(seed=_rng)
                log_prob_1 = pi_1.log_prob(action_1)

                # Now create agent_0's observation with agent_1's actual action
                one_hot_action = jax.nn.one_hot(action_1, env.action_space().n)
                agent_0_obs_augmented = jnp.concatenate([agent_0_obs, one_hot_action], axis=-1)
                
                # Get agent_0's action using the same shared parameters
                pi_0, value_0 = network.apply(train_state.params, agent_0_obs_augmented)
                action_0 = pi_0.sample(seed=_rng)
                log_prob_0 = pi_0.log_prob(action_0)

                # Combine actions for environment step
                action = jnp.stack([action_0, action_1])
                value = jnp.stack([value_0, value_1])
                log_prob = jnp.stack([log_prob_0, log_prob_1])

                # Store processed observations
                processed_obs = {
                    'agent_0': agent_0_obs_augmented,
                    'agent_1': agent_1_obs_augmented
                }

                # Package actions for environment step
                env_act = {
                    "agent_0": action_0,
                    "agent_1": action_1
                }
                env_act = {k: v.flatten() for k, v in env_act.items()}

                # STEP ENV
                rng, _rng = jax.random.split(rng)
                rng_step = jax.random.split(_rng, config["NUM_ENVS"])
                
                obsv, env_state, reward, done, info = jax.vmap(env.step, in_axes=(0,0,0))(
                    rng_step, env_state, env_act
                )

                print("reward:", reward)
                print("shaped reward:", info["shaped_reward"])

                info["reward"] = reward["agent_0"]

                current_timestep = update_step*config["NUM_STEPS"]*config["NUM_ENVS"]
                reward = jax.tree.map(lambda x,y: x+y*rew_shaping_anneal(current_timestep), reward, info["shaped_reward"])
                
                # Create transition with consistent ordering
                transition = Transition(
                    done=jnp.array([done["agent_1"], done["agent_0"]]).squeeze(),
                    action=jnp.array([action_1, action_0]),
                    value=jnp.array([value_1, value_0]),
                    reward=jnp.array([
                        reward["agent_1"],
                        reward["agent_0"]
                    ]).squeeze(),
                    log_prob=jnp.array([log_prob_1, log_prob_0]),
                    obs=processed_obs
                )

                runner_state = (train_state, env_state, obsv, update_step, rng)
                return runner_state, (transition, info, processed_obs)
            
            runner_state, (traj_batch, info) = jax.lax.scan(
                _env_step, runner_state, None, config["NUM_STEPS"]
            )
            
            # CALCULATE ADVANTAGE
            train_state, env_state, last_obs, update_step, rng = runner_state
            last_obs_batch = batchify(last_obs, env.agents, config["NUM_ACTORS"], env_dims["action_dim"])
            _, last_val = network.apply(train_state.params, last_obs_batch)

            # This function calculates the advantage for each transition in the trajectory (basically, policy optimization).
            # It returns the advantages and value targets.
            def _calculate_gae(traj_batch, last_val):
                """Calculate Generalized Advantage Estimation (GAE) for trajectories.
                
                This function computes the GAE for a given trajectory batch and last value,
                which are used to estimate the advantage of each action in the trajectory.

                Args:
                    traj_batch: Trajectory batch containing transitions
                    last_val: Last value estimates for the trajectory
                    
                Returns:
                    Tuple containing:
                        - Advantages for the trajectory
                        - Returns (advantages + value estimates)
                """
                # Inner function that processes one transition at a time
                print(f"\nGAE Calculation Debug:")
                print("traj_batch types:", jax.tree_map(lambda x: x.dtype, traj_batch))
                print(f"traj_batch shapes:", jax.tree_map(lambda x: x.shape, traj_batch))
                print("last_val types:", jax.tree_map(lambda x: x.dtype, last_val))
                print(f"last_val shape: {last_val.shape}")
                
                # This function calculates the advantage for each transition in the trajectory.
                def _get_advantages(gae_and_next_value, transition):
                    """Calculate GAE and returns for a single transition.
                    
                    This function computes the GAE and returns for a single transition,
                    which are used to update the policy and value functions.
                    
                    Args:
                        gae_and_next_value: Tuple containing current GAE and next value
                        transition: Single transition containing data for one step
                    
                    Returns:
                        Tuple containing:
                            - Updated GAE and next value
                            - Calculated GAE
                    """
                    # Unpack the carried state (previous GAE and next state's value)
                    gae, next_value = gae_and_next_value
                    # Get current transition info
                    done, value, reward = (
                        transition.done,
                        transition.value,
                        transition.reward,
                    )

                    # Debug intermediate calculations
                    print(f"\nGAE step debug:")
                    print(f"done shape: {done.shape}")
                    print(f"value shape: {value.shape}")
                    print(f"reward shape: {reward.shape}")
                    print(f"next_value shape: {next_value.shape}")
                    print(f"gae shape: {gae.shape}")

                    # # Reshape done and reward to match per-agent structure
                    # done = done.reshape(2, 16, 7)    # (2 agents, 16 envs, 7 features)
                    # reward = reward.reshape(2, 16, 7) # Same shape as done
                    # value = value.reshape(2, 16)      # (2 agents, 16 envs)
                    # next_value = next_value.reshape(2, 16) # Same as value

                    # Calculate TD error (temporal difference)
                    # δt = rt + γV(st+1) - V(st)
                    delta = reward + config["GAMMA"] * next_value * (1 - done) - value
                    print(f"delta shape: {delta.shape}, value: {delta}")

                    # Calculate GAE using the recursive formula:
                    # At = δt + (γλ)At+1
                    # (1 - done) ensures GAE is zero for terminal states
                    gae = (
                        delta
                        + config["GAMMA"] * config["GAE_LAMBDA"] * (1 - done) * gae
                    )
                    print(f"calculated gae shape: {gae.shape}, value: {gae}")

                    # Return the updated GAE and the next state's value
                    return (gae, value), gae

                # Use scan to process the trajectory backwards
                _, advantages = jax.lax.scan(
                    _get_advantages,
                    (jnp.zeros_like(last_val), last_val), # Initial GAE and the final value
                    traj_batch, # Sequence of transitions
                    reverse=True, # Process the trajectory backwards
                    unroll=16, # Unroll optimization
                )
                # Return advantages and value targets
                # Value targets = advantages + value estimates
                # Calculate returns (advantages + value estimates)
                print(f"\nFinal shapes:")
                print(f"advantages shape: {advantages.shape}")
                print(f"returns shape: {(advantages + traj_batch.value).shape}")
                return advantages, advantages + traj_batch.value

            advantages, targets = _calculate_gae(traj_batch, last_val)
            
            # UPDATE NETWORK
            # This function performs multiple optimization steps on the collected trajectories.
            def _update_epoch(update_state, unused):
                """
                Performs a complete training epoch.
                
                Args:
                    update_state: Tuple containing (train_state, traj_batch, advantages, targets, rng)
                    unused: Placeholder for scan compatibility
                    
                Returns:
                    Updated state and loss information
                """
                def _update_minbatch(train_state, batch_info):
                    """Updates network parameters using a minibatch of experience.
                    
                    Args:
                        train_state: Current training state containing both agents' parameters
                        batch_info: Tuple of (traj_batch, advantages, targets) for both agents
                        
                    Returns:
                        Updated training state and loss information
                    """
                    traj_batch, advantages, targets = batch_info
                    print("Minibatch shapes:")
                    print(f"traj_batch: {jax.tree_map(lambda x: x.shape, traj_batch)}")
                    print(f"advantages: {advantages.shape}")
                    print(f"targets: {targets.shape}")

                    def _loss_fn(params, traj_batch, gae, targets):
                        """Calculate loss for a single agent.
                        
                        This function computes the loss for a single agent, which is used
                        to update the policy and value functions.
                        
                        Args:
                            params: Network parameters for the agent
                            traj_batch: Trajectory batch containing transitions
                            gae: Generalized Advantage Estimation (GAE) for the trajectory
                            targets: Target values (advantages + value estimates) for the trajectory
                        
                        Returns:
                            Tuple containing:
                                - Total loss for the agent
                                - Auxiliary loss information (value loss, actor loss, entropy)
                        """
                        print("\nCalculating losses...")
                        # RERUN NETWORK
                        pi, value = network.apply(params, traj_batch.obs)
                        print(f"Network outputs - pi shape: {pi.batch_shape}, value shape: {value.shape}")
                        log_prob = pi.log_prob(traj_batch.action)
                        print(f"Log prob shape: {log_prob.shape}")

                        # CALCULATE VALUE LOSS
                        value_pred_clipped = traj_batch.value + (
                            value - traj_batch.value
                        ).clip(-config["CLIP_EPS"], config["CLIP_EPS"])
                        value_losses = jnp.square(value - targets)
                        value_losses_clipped = jnp.square(value_pred_clipped - targets)
                        value_loss = (
                            0.5 * jnp.maximum(value_losses, value_losses_clipped).mean()
                        )
                        print(f"Value loss: {value_loss}")

                        # CALCULATE ACTOR LOSS
                        ratio = jnp.exp(log_prob - traj_batch.log_prob)
                        print(f"Importance ratio shape: {ratio.shape}")
                        gae = (gae - gae.mean()) / (gae.std() + 1e-8)
                        print(f"Normalized GAE shape: {gae.shape}")
                        loss_actor1 = ratio * gae
                        loss_actor2 = (
                            jnp.clip(
                                ratio,
                                1.0 - config["CLIP_EPS"],
                                1.0 + config["CLIP_EPS"],
                            )
                            * gae
                        )
                        loss_actor = -jnp.minimum(loss_actor1, loss_actor2)
                        loss_actor = loss_actor.mean()
                        entropy = pi.entropy().mean()
                        print(f"Actor loss: {loss_actor}, Entropy: {entropy}")

                        total_loss = (
                            loss_actor
                            + config["VF_COEF"] * value_loss
                            - config["ENT_COEF"] * entropy
                        )
                        print(f"Total loss: {total_loss}")
                        
                        loss_info = {
                            'value_loss': value_loss,
                            'actor_loss': loss_actor,
                            'entropy': entropy,
                            'total_loss': total_loss,
                            'grad_norm': None  # Will be filled later
                        }

                        print(f"\nLoss breakdown:")
                        for k, v in loss_info.items():
                            if v is not None:
                                print(f"{k}: {v}")

                        return total_loss, (value_loss, loss_actor, entropy)

                    grad_fn = jax.value_and_grad(_loss_fn, has_aux=True)
                    total_loss, grads = grad_fn(
                        train_state.params, traj_batch, advantages, targets
                    )
                    print("\nGradient stats:")
                    print(f"Grad norm: {optax.global_norm(grads)}")
                    train_state = train_state.apply_gradients(grads=grads)
                    return train_state, total_loss

                train_state, traj_batch, advantages, targets, rng = update_state
                rng, _rng = jax.random.split(rng)
                # Calculate batch size
                batch_size = config["MINIBATCH_SIZE"] * config["NUM_MINIBATCHES"]
                assert (
                    batch_size == config["NUM_STEPS"] * config["NUM_ACTORS"]
                ), "batch size must be equal to number of steps * number of actors"
                
                # Create permutation for shuffling
                permutation = jax.random.permutation(_rng, batch_size)
                batch = (traj_batch, advantages, targets)

                print("\nBatch processing:")
                print("batch_size:", batch_size)
                print("Original batch structure:", jax.tree_map(lambda x: x.shape, batch))
                
                # Reshape batch to match minibatch size
                batch = jax.tree_map(
                    lambda x: (print(f"Reshaping {x.shape} to {(batch_size,) + x.shape[2:]}"), 
                            x.reshape((batch_size,) + x.shape[2:]))[1],
                    batch
                )
                print("Reshaped batch structure:", jax.tree_map(lambda x: x.shape, batch))
                
                # Shuffle batch
                shuffled_batch = jax.tree.map(
                    lambda x: jnp.take(x, permutation, axis=0), batch
                )
                print("Shuffled batch structure:", jax.tree_map(lambda x: x.shape, shuffled_batch))
                
                # Create minibatches
                minibatches = jax.tree.map(
                    lambda x: jnp.reshape(
                        x, [config["NUM_MINIBATCHES"], -1] + list(x.shape[1:])
                    ),
                    shuffled_batch,
                )
                print("Minibatches structure:", jax.tree_map(lambda x: x.shape, minibatches))
                
                # Update network parameters using minibatches
                train_state, total_loss = jax.lax.scan(
                    _update_minbatch, train_state, minibatches
                )
                update_state = (train_state, traj_batch, advantages, targets, rng)
                return update_state, total_loss

            # Perform a complete training epoch
            update_state = (train_state, traj_batch, advantages, targets, rng)
            update_state, loss_info = jax.lax.scan(
                _update_epoch, update_state, None, config["UPDATE_EPOCHS"]
            )
            train_state = update_state[0]
            metric = info
            current_timestep = update_step*config["NUM_STEPS"]*config["NUM_ENVS"]
            metric["shaped_reward"] = metric["shaped_reward"]["agent_0"]
            metric["shaped_reward_annealed"] = metric["shaped_reward"]*rew_shaping_anneal(current_timestep)
            
            rng = update_state[-1]

            def callback(metric):
                """Log training metrics to wandb.
                
                This function logs the training metrics to wandb, which are used for
                monitoring and analysis during training.

                Args:
                    metric: Training metrics to be logged
                """
                wandb.log(
                    metric
                )
            update_step = update_step + 1
            metric = jax.tree.map(lambda x: x.mean(), metric)
            metric["update_step"] = update_step
            metric["env_step"] = update_step*config["NUM_STEPS"]*config["NUM_ENVS"]
            jax.debug.callback(callback, metric)
            
            runner_state = (train_state, env_state, last_obs, update_step, rng)
            return runner_state, metric

        rng, _rng = jax.random.split(rng)
        runner_state = (train_state, env_state, obsv, 0, _rng)
        runner_state, metric = jax.lax.scan(
            _update_step, runner_state, None, config["NUM_UPDATES"]
        )
        return {"runner_state": runner_state, "metrics": metric}

    return train

@hydra.main(version_base=None, config_path="config", config_name="ippo_ff_overcooked_oracle")
def main(config):
    """Main entry point for training
    
    Args:
        config: Hydra configuration object containing training parameters
        
    Returns:
        Training results and metrics
    
    Raises:
        ValueError: If the environment dimensions are invalid
    """
    # Validate config
    required_keys = ["ENV_NAME", "ENV_KWARGS"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")

    # Process config
    config = OmegaConf.to_container(config) 
    layout_name = config["ENV_KWARGS"]["layout"]
    config["ENV_KWARGS"]["layout"] = overcooked_layouts[layout_name]

    # Create environment using JaxMARL framework
    env = jaxmarl.make(config["ENV_NAME"], **config["ENV_KWARGS"])

    # Get environment dimensions
    base_obs_shape = env.observation_space().shape
    base_obs_dim = int(np.prod(base_obs_shape))
    action_dim = int(env.action_space().n)
    augmented_obs_dim = base_obs_dim + action_dim

    # Validate dimensions
    assert base_obs_dim > 0, f"Invalid base observation dimension: {base_obs_dim}"
    assert action_dim > 0, f"Invalid action dimension: {action_dim}"
    assert augmented_obs_dim > base_obs_dim, "Augmented dim must be larger than base dim"

    # Store dimensions in config for easy access
    config["DIMS"] = {
        "base_obs_shape": base_obs_shape,
        "base_obs_dim": base_obs_dim,
        "action_dim": action_dim,
        "augmented_obs_dim": augmented_obs_dim
    }

    # Initialize wandb logging
    wandb.init(
        entity=config["ENTITY"],
        project=config["PROJECT"],
        tags=["IPPO", "FF", "Debug", "Oracle", "Params-shared"],
        config=config,
        mode=config["WANDB_MODE"],
        name=f'ippo_ff_overcooked_{layout_name}'
    )

    # Setup random seeds and training
    rng = jax.random.PRNGKey(config["SEED"])
    rngs = jax.random.split(rng, config["NUM_SEEDS"])    
    train_jit = jax.jit(make_train(config))
    out = jax.vmap(train_jit)(rngs)

    print("\nVerifying config before rollout:")
    print("Config keys:", config.keys())
    if "DIMS" in config:
        print("Found dimensions:")
        for key, value in config["DIMS"].items():
            print(f"  {key}: {value}")
    else:
        raise ValueError("DIMS not found in config - check dimension initialization")


    # Generate visualization
    filename = f'{config["ENV_NAME"]}_{layout_name}'
    train_state = jax.tree.map(lambda x: x[0], out["runner_state"][0])
    state_seq = get_rollout(train_state, config)
    viz = OvercookedVisualizer()
    # agent_view_size is hardcoded as it determines the padding around the layout.
    viz.animate(state_seq, agent_view_size=5, filename=f"{filename}.gif")
    
    
    """
    print('** Saving Results **')
    filename = f'{config["ENV_NAME"]}_cramped_room_new'
    rewards = out["metrics"]["returned_episode_returns"].mean(-1).reshape((num_seeds, -1))
    reward_mean = rewards.mean(0)  # mean 
    reward_std = rewards.std(0) / np.sqrt(num_seeds)  # standard error
    
    plt.plot(reward_mean)
    plt.fill_between(range(len(reward_mean)), reward_mean - reward_std, reward_mean + reward_std, alpha=0.2)
    # compute standard error
    plt.xlabel("Update Step")
    plt.ylabel("Return")
    plt.savefig(f'{filename}.png')

    # animate first seed
    train_state = jax.tree.map(lambda x: x[0], out["runner_state"][0])
    state_seq = get_rollout(train_state, config)
    viz = OvercookedVisualizer()
    # agent_view_size is hardcoded as it determines the padding around the layout.
    viz.animate(state_seq, agent_view_size=5, filename=f"{filename}.gif")
    """

if __name__ == "__main__":
    main()