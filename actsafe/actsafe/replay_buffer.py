from typing import Iterator
import jax
import numpy as np

from actsafe.common.double_buffer import double_buffer
from actsafe.rl.trajectory import TrajectoryData


class ReplayBuffer:
    def __init__(
        self,
        observation_shape: tuple[int, ...],
        action_shape: tuple[int, ...],
        max_length: int,
        seed: int,
        capacity: int,
        batch_size: int,
        sequence_length: int,
        num_rewards: int,
    ):
        self.episode_id = 0
        self.dtype = np.float32

        if len(observation_shape)>2:#img
            self.obs_dtype = np.uint8
        else:# vector
            self.obs_dtype = np.float32

        self.observation = np.zeros(
            (
                capacity,
                max_length,
            )
            + observation_shape,
            dtype=self.obs_dtype,
        )
        self.action = np.zeros(
            (
                capacity,
                max_length,
            )
            + action_shape,
            dtype=self.dtype,
        )
        self.reward = np.zeros(
            (capacity, max_length, num_rewards),
            dtype=self.dtype,
        )
        self.cost = np.zeros(
            (
                capacity,
                max_length,
            ),
            dtype=self.dtype,
        )
        self.terminal = np.zeros(
            (
                capacity,
                max_length,
            ),
            dtype=self.dtype,
        )
        self._valid_episodes = 0
        self.rs = np.random.RandomState(seed)
        self.batch_size = batch_size
        self.sequence_length = sequence_length

    def add_batch(self, trajectory: TrajectoryData):
        capacity, *_ = self.reward.shape
        batch_size = min(trajectory.observation.shape[0], capacity)
        # Discard data if batch size overflows capacity.
        end = min(self.episode_id + batch_size, capacity)
        episode_slice = slice(self.episode_id, end)
        if trajectory.reward.ndim == 2:
            trajectory = TrajectoryData(
                trajectory.observation,
                trajectory.next_observation,
                trajectory.action,
                trajectory.reward[..., None],
                trajectory.cost,
                trajectory.done,
                trajectory.terminal,
            )
        for data, val in zip(
            (self.action, self.reward, self.cost, self.terminal),
            (
                trajectory.action,
                trajectory.reward,
                trajectory.cost,
                trajectory.terminal,
            ),
        ):
            episode_length = val.shape[1]
            data[episode_slice, :episode_length] = val[:batch_size].astype(self.dtype)
        episode_length = trajectory.observation.shape[1]
        self.observation[episode_slice, :episode_length] = trajectory.observation[:batch_size].astype(
            self.obs_dtype
        )
        self.episode_id = (self.episode_id + batch_size) % capacity
        self._valid_episodes = min(self._valid_episodes + batch_size, capacity)

    def _sample_batch_idx(self, batch_size, sequence_length):
        valid_episodes, valid_episodes_lengths = self._get_valid_episodes()
        timesteps = []
        episodes = []
        for _ in range(batch_size):
            episode_id = self.rs.choice(valid_episodes)
            episodes.append(episode_id)
            low = self.rs.randint(
                valid_episodes_lengths[episode_id] - sequence_length
            )
            timestep_ids = low + np.arange(sequence_length + 1)
            timesteps.append(timestep_ids)
        return np.asarray(episodes), np.asarray(timesteps)

    def _get_valid_episodes(self):
        time_limit = self.observation.shape[1]
        first_terminal = self.terminal.argmax(axis=1)
        max_length = np.where(first_terminal == 0, time_limit, first_terminal)
        valid_episodes_lengths = max_length[: self._valid_episodes]
        valid_episodes = np.where(valid_episodes_lengths >= self.sequence_length + 1)[0]
        return valid_episodes, valid_episodes_lengths

    def _sample_batch(
        self,
        batch_size: int,
        sequence_length: int,
    ):
        time_limit = self.observation.shape[1]
        assert time_limit > sequence_length
        while True:
            episode_ids, timestep_ids = self._sample_batch_idx(
                batch_size, sequence_length
            )
            # Sample a sequence of length H for the actions, rewards and costs,
            # and a length of H + 1 for the observations (which is needed for
            # bootstrapping)
            a, r, c, t = [
                x[episode_ids[:, None], timestep_ids[:, :-1]]
                for x in (self.action, self.reward, self.cost, self.terminal)
            ]
            o = self.observation[episode_ids[:, None], timestep_ids]
            o, next_o = o[:, :-1], o[:, 1:]
            yield o, next_o, a, r, c, t, t

    def sample(self, n_batches: int) -> Iterator[TrajectoryData]:
        if self.empty:
            return
        iterator = (
            TrajectoryData(
                *next(self._sample_batch(self.batch_size, self.sequence_length))
            )  # type: ignore
            for _ in range(n_batches)
        )
        if jax.default_backend() == "gpu":
            iterator = double_buffer(iterator)  # type: ignore
        yield from iterator

    @property
    def empty(self):
        if self._valid_episodes != 0:
            valid_episodes, _ = self._get_valid_episodes()
            return len(valid_episodes) == 0
        return True
