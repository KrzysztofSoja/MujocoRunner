from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

import gym
import torch as th
from torch import nn

from  stable_baselines3 import SAC
from stable_baselines3.common.distributions import SquashedDiagGaussianDistribution, StateDependentNoiseDistribution
from stable_baselines3.common.policies import BasePolicy, create_sde_features_extractor, register_policy
from stable_baselines3.common.preprocessing import get_action_dim
from stable_baselines3.common.torch_layers import (
    BaseFeaturesExtractor,
    FlattenExtractor,
    create_mlp,
    get_actor_critic_arch,
)
from stable_baselines3.sac.policies import SACPolicy as BaseSACPolicy

from common.torch_layers import create_mlp_with_dropout
from common.policies import ContinuousCritic

# CAP the standard deviation of the actor
LOG_STD_MAX = 2
LOG_STD_MIN = -20


class Actor(BasePolicy):
    """
    Actor network (policy) for SAC.

    :param observation_space: Obervation space
    :param action_space: Action space
    :param net_arch: Network architecture
    :param features_extractor: Network to extract features
        (a CNN when using images, a nn.Flatten() layer otherwise)
    :param features_dim: Number of features
    :param activation_fn: Activation function
    :param use_sde: Whether to use State Dependent Exploration or not
    :param log_std_init: Initial value for the log standard deviation
    :param full_std: Whether to use (n_features x n_actions) parameters
        for the std instead of only (n_features,) when using gSDE.
    :param sde_net_arch: Network architecture for extracting features
        when using gSDE. If None, the latent features from the policy will be used.
        Pass an empty list to use the states as features.
    :param use_expln: Use ``expln()`` function instead of ``exp()`` when using gSDE to ensure
        a positive standard deviation (cf paper). It allows to keep variance
        above zero and prevent it from growing too fast. In practice, ``exp()`` is usually enough.
    :param clip_mean: Clip the mean output when using gSDE to avoid numerical instability.
    :param normalize_images: Whether to normalize images or not,
         dividing by 255.0 (True by default)
    """

    def __init__(
        self,
        observation_space: gym.spaces.Space,
        action_space: gym.spaces.Space,
        net_arch: List[int],
        features_extractor: nn.Module,
        features_dim: int,
        activation_fn: Type[nn.Module] = nn.ReLU,
        create_network: callable = create_mlp,
        dropout_rate: Optional[float] = .5,
        use_sde: bool = False,
        log_std_init: float = -3,
        full_std: bool = True,
        sde_net_arch: Optional[List[int]] = None,
        use_expln: bool = False,
        clip_mean: float = 2.0,
        normalize_images: bool = True,
    ):
        super(Actor, self).__init__(
            observation_space,
            action_space,
            features_extractor=features_extractor,
            normalize_images=normalize_images,
            squash_output=True,
        )

        # Save arguments to re-create object at loading
        self.use_sde = use_sde
        self.sde_features_extractor = None
        self.sde_net_arch = sde_net_arch
        self.net_arch = net_arch
        self.features_dim = features_dim
        self.activation_fn = activation_fn
        self.log_std_init = log_std_init
        self.sde_net_arch = sde_net_arch
        self.use_expln = use_expln
        self.full_std = full_std
        self.clip_mean = clip_mean

        action_dim = get_action_dim(self.action_space)
        if create_network.__name__ == create_mlp_with_dropout.__name__:
            latent_pi_net = create_network(features_dim, -1, net_arch, activation_fn, dropout_rate=dropout_rate)
        else:
            latent_pi_net = create_network(features_dim, -1, net_arch, activation_fn)


        self.latent_pi = nn.Sequential(*latent_pi_net)
        last_layer_dim = net_arch[-1] if len(net_arch) > 0 else features_dim

        if self.use_sde:
            latent_sde_dim = last_layer_dim
            # Separate features extractor for gSDE
            if sde_net_arch is not None:
                self.sde_features_extractor, latent_sde_dim = create_sde_features_extractor(
                    features_dim, sde_net_arch, activation_fn
                )

            self.action_dist = StateDependentNoiseDistribution(
                action_dim, full_std=full_std, use_expln=use_expln, learn_features=True, squash_output=True
            )
            self.mu, self.log_std = self.action_dist.proba_distribution_net(
                latent_dim=last_layer_dim, latent_sde_dim=latent_sde_dim, log_std_init=log_std_init
            )
            # Avoid numerical issues by limiting the mean of the Gaussian
            # to be in [-clip_mean, clip_mean]
            if clip_mean > 0.0:
                self.mu = nn.Sequential(self.mu, nn.Hardtanh(min_val=-clip_mean, max_val=clip_mean))
        else:
            self.action_dist = SquashedDiagGaussianDistribution(action_dim)
            self.mu = nn.Linear(last_layer_dim, action_dim)
            self.log_std = nn.Linear(last_layer_dim, action_dim)

    def _get_data(self) -> Dict[str, Any]:
        data = super()._get_data()

        data.update(
            dict(
                net_arch=self.net_arch,
                features_dim=self.features_dim,
                activation_fn=self.activation_fn,
                use_sde=self.use_sde,
                log_std_init=self.log_std_init,
                full_std=self.full_std,
                sde_net_arch=self.sde_net_arch,
                use_expln=self.use_expln,
                features_extractor=self.features_extractor,
                clip_mean=self.clip_mean,
            )
        )
        return data

    def get_std(self) -> th.Tensor:
        """
        Retrieve the standard deviation of the action distribution.
        Only useful when using gSDE.
        It corresponds to ``th.exp(log_std)`` in the normal case,
        but is slightly different when using ``expln`` function
        (cf StateDependentNoiseDistribution doc).

        :return:
        """
        msg = "get_std() is only available when using gSDE"
        assert isinstance(self.action_dist, StateDependentNoiseDistribution), msg
        return self.action_dist.get_std(self.log_std)

    def reset_noise(self, batch_size: int = 1) -> None:
        """
        Sample new weights for the exploration matrix, when using gSDE.

        :param batch_size:
        """
        msg = "reset_noise() is only available when using gSDE"
        assert isinstance(self.action_dist, StateDependentNoiseDistribution), msg
        self.action_dist.sample_weights(self.log_std, batch_size=batch_size)

    def get_action_dist_params(self, obs: th.Tensor) -> Tuple[th.Tensor, th.Tensor, Dict[str, th.Tensor]]:
        """
        Get the parameters for the action distribution.

        :param obs:
        :return:
            Mean, standard deviation and optional keyword arguments.
        """
        features = self.extract_features(obs)
        latent_pi = self.latent_pi(features)
        mean_actions = self.mu(latent_pi)

        if self.use_sde:
            latent_sde = latent_pi
            if self.sde_features_extractor is not None:
                latent_sde = self.sde_features_extractor(features)
            return mean_actions, self.log_std, dict(latent_sde=latent_sde)
        # Unstructured exploration (Original implementation)
        log_std = self.log_std(latent_pi)
        # Original Implementation to cap the standard deviation
        log_std = th.clamp(log_std, LOG_STD_MIN, LOG_STD_MAX)
        return mean_actions, log_std, {}

    def forward(self, obs: th.Tensor, deterministic: bool = False) -> th.Tensor:
        mean_actions, log_std, kwargs = self.get_action_dist_params(obs)
        # Note: the action is squashed
        return self.action_dist.actions_from_params(mean_actions, log_std, deterministic=deterministic, **kwargs)

    def action_log_prob(self, obs: th.Tensor) -> Tuple[th.Tensor, th.Tensor]:
        mean_actions, log_std, kwargs = self.get_action_dist_params(obs)
        # return action and associated log prob
        return self.action_dist.log_prob_from_params(mean_actions, log_std, **kwargs)

    def _predict(self, observation: th.Tensor, deterministic: bool = False) -> th.Tensor:
        return self.forward(observation, deterministic)


class SACPolicy(BaseSACPolicy):
    """
    Policy class (with both actor and critic) for SAC.

    :param observation_space: Observation space
    :param action_space: Action space
    :param lr_schedule: Learning rate schedule (could be constant)
    :param net_arch: The specification of the policy and value networks.
    :param activation_fn: Activation function
    :param use_sde: Whether to use State Dependent Exploration or not
    :param log_std_init: Initial value for the log standard deviation
    :param sde_net_arch: Network architecture for extracting features
        when using gSDE. If None, the latent features from the policy will be used.
        Pass an empty list to use the states as features.
    :param use_expln: Use ``expln()`` function instead of ``exp()`` when using gSDE to ensure
        a positive standard deviation (cf paper). It allows to keep variance
        above zero and prevent it from growing too fast. In practice, ``exp()`` is usually enough.
    :param clip_mean: Clip the mean output when using gSDE to avoid numerical instability.
    :param features_extractor_class: Features extractor to use.
    :param features_extractor_kwargs: Keyword arguments
        to pass to the features extractor.
    :param normalize_images: Whether to normalize images or not,
         dividing by 255.0 (True by default)
    :param optimizer_class: The optimizer to use,
        ``th.optim.Adam`` by default
    :param optimizer_kwargs: Additional keyword arguments,
        excluding the learning rate, to pass to the optimizer
    :param n_critics: Number of critic networks to create.
    :param share_features_extractor: Whether to share or not the features extractor
        between the actor and the critic (this saves computation time)
    """

    def __init__(
        self,
        observation_space: gym.spaces.Space,
        action_space: gym.spaces.Space,
        lr_schedule: Callable,
        net_arch: Optional[Union[List[int], Dict[str, List[int]]]] = None,
        activation_fn: Type[nn.Module] = nn.ReLU,
        use_sde: bool = False,
        log_std_init: float = -3,
        sde_net_arch: Optional[List[int]] = None,
        use_expln: bool = False,
        clip_mean: float = 2.0,
        features_extractor_class: Type[BaseFeaturesExtractor] = FlattenExtractor,
        features_extractor_kwargs: Optional[Dict[str, Any]] = None,
        create_network_function: callable = create_mlp,
        dropout_rate: Optional[float] = None,
        weight_decay: Optional[float] = 0.0,
        normalize_images: bool = True,
        optimizer_class: Type[th.optim.Optimizer] = th.optim.Adam,
        optimizer_kwargs: Optional[Dict[str, Any]] = None,
        n_critics: int = 2,
        share_features_extractor: bool = True,
    ):

        self.create_network_function = create_network_function
        if self.create_network_function.__name__ == create_mlp_with_dropout.__name__:
            self.dropout_rate = .5 if dropout_rate is None else dropout_rate

        if weight_decay != 0:
            if optimizer_kwargs is None:
                optimizer_kwargs = dict()
            optimizer_kwargs['weight_decay'] = weight_decay

        super(BaseSACPolicy, self).__init__(
            observation_space,
            action_space,
            features_extractor_class,
            features_extractor_kwargs,
            optimizer_class=optimizer_class,
            optimizer_kwargs=optimizer_kwargs,
            squash_output=True,
        )

        if net_arch is None:
            if features_extractor_class == FlattenExtractor:
                net_arch = [256, 256]
            else:
                net_arch = []

        actor_arch, critic_arch = get_actor_critic_arch(net_arch)

        self.net_arch = net_arch
        self.activation_fn = activation_fn
        self.net_args = {
            "observation_space": self.observation_space,
            "action_space": self.action_space,
            "net_arch": actor_arch,
            "activation_fn": self.activation_fn,
            "normalize_images": normalize_images,
        }
        self.actor_kwargs = self.net_args.copy()
        sde_kwargs = {
            "use_sde": use_sde,
            "log_std_init": log_std_init,
            "sde_net_arch": sde_net_arch,
            "use_expln": use_expln,
            "clip_mean": clip_mean,
        }
        self.actor_kwargs.update(sde_kwargs)
        self.critic_kwargs = self.net_args.copy()
        self.critic_kwargs.update(
            {
                "n_critics": n_critics,
                "net_arch": critic_arch,
                "share_features_extractor": share_features_extractor,
            }
        )

        self.actor, self.actor_target = None, None
        self.critic, self.critic_target = None, None
        self.share_features_extractor = share_features_extractor

        self._build(lr_schedule)

    def make_actor(self, features_extractor: Optional[BaseFeaturesExtractor] = None) -> Actor:
        actor_kwargs = self._update_features_extractor(self.actor_kwargs, features_extractor)
        if self.create_network_function.__name__ == create_mlp_with_dropout.__name__:
            actor_kwargs['dropout_rate'] = self.dropout_rate
        return Actor(create_network=self.create_network_function, **actor_kwargs).to(self.device)

    def make_critic(self, features_extractor: Optional[BaseFeaturesExtractor] = None) -> ContinuousCritic:
        critic_kwargs = self._update_features_extractor(self.critic_kwargs, features_extractor)
        if self.create_network_function.__name__ == create_mlp_with_dropout.__name__:
            critic_kwargs['dropout_rate'] = self.dropout_rate
        return ContinuousCritic(create_network=self.create_network_function, **critic_kwargs).to(self.device)


MlpPolicy = SACPolicy

if __name__ == '__main__':
    model = SAC(SACPolicy, "Pendulum-v0",
                policy_kwargs={'create_network_function': create_mlp_with_dropout,
                               'dropout_rate': 0.5,
                               'weight_decay': 0.5},
                verbose=1)
    print(model.policy.actor.optimizer)
    model.learn(10_000)
