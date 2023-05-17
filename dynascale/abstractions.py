import itertools
from abc import ABC, abstractmethod

import numpy as np
import pandas as pd
from tqdm.auto import tqdm


class Model(ABC):
    def __init__(self, embed_dim, timesteps, max_control_cost, **kwargs):
        self._embed_dim = embed_dim
        self._timesteps = timesteps  # NOTE: this is the timesteps of the training data; NOT the predicted trajectories
        self._max_control_cost = max_control_cost

    @abstractmethod
    def fit(self, x: np.ndarray, **kwargs):
        raise NotImplementedError

    def _act(self, x: np.ndarray, *args, **kwargs) -> np.ndarray:
        return np.zeros_like(x)

    def act(self, x: np.ndarray, *args, **kwargs) -> np.ndarray:
        control = self._act(x, *args, **kwargs)
        assert control.shape == x.shape
        return control

    @abstractmethod
    def _predict(self, x0: np.ndarray, timesteps: int, *args, **kwargs) -> np.ndarray:
        raise NotImplementedError

    def predict(self, x0: np.ndarray, timesteps, *args, **kwargs) -> np.ndarray:
        pred = self._predict(x0, timesteps, *args, **kwargs)
        n = x0.shape[0]
        assert pred.shape == (n, self._timesteps, self._embed_dim)
        return pred


class Challenge(ABC):
    def __init__(self, latent_dim, embed_dim):
        self._latent_dim = latent_dim
        self._embed_dim = embed_dim

    @property
    def latent_dim(self):
        return self._latent_dim

    @property
    def embed_dim(self):
        return self._embed_dim

    @latent_dim.setter
    def latent_dim(self, value):
        self._latent_dim = value

    @embed_dim.setter
    def embed_dim(self, value):
        self._embed_dim = value

    @abstractmethod
    def _make_init_conds(self, n: int, in_dist=True) -> np.ndarray:
        raise NotImplementedError

    def make_init_conds(self, n: int, in_dist=True):
        init_conds = self._make_init_conds(n, in_dist)
        assert init_conds.shape == (n, self.embed_dim)
        return init_conds

    @abstractmethod
    def _make_data(self, init_conds: np.ndarray, control: np.ndarray, timesteps: int, noisy=False) -> np.ndarray:
        raise NotImplementedError

    def make_data(self, init_conds: np.ndarray, control: np.ndarray = None, timesteps: int = 1,
                  noisy=False) -> np.ndarray:
        assert timesteps > 0
        assert init_conds.ndim == 2 and init_conds.shape[1] == self.embed_dim
        n = init_conds.shape[0]
        if control is None:
            control = np.zeros((n, timesteps, self.embed_dim))
        assert control.shape == (n, timesteps, self.embed_dim)
        data = self._make_data(init_conds=init_conds, control=control, timesteps=timesteps, noisy=noisy)
        assert data.shape == (n, timesteps, self.embed_dim)
        return data

    @abstractmethod
    def _calc_loss(self, x, y) -> float:
        raise NotImplementedError

    def calc_loss(self, x, y) -> float:
        assert x.shape == y.shape
        return self._calc_loss(x, y)

    @abstractmethod
    def _calc_control_cost(self, control: np.ndarray) -> float:
        raise NotImplementedError

    def calc_control_cost(self, control: np.ndarray) -> float:
        assert control.shape[2] == self.embed_dim and control.ndim == 3
        cost = self._calc_control_cost(control)
        return cost


class Task:
    def __init__(self,
                 N: list[int],
                 L: list[int],
                 E: list[int],
                 T: list[int],
                 C: list[float],
                 control_horizons: int,
                 challenge_cls: type[Challenge],
                 reps: int,
                 test_examples: int,
                 test_timesteps: int,
                 challenge_kwargs: dict = None,
                 ):
        assert control_horizons > 0

        self._id = itertools.count()
        self._N = N
        self._L = L
        self._E = E
        self._T = T
        self._C = C
        self._challenge_cls = challenge_cls
        self._challenge_kwargs = challenge_kwargs or {}
        self._control_horizons = control_horizons
        self._reps = reps
        self._test_examples = test_examples
        self._test_timesteps = test_timesteps

    def evaluate(self, model_cls: type[Model],
                 model_kwargs: dict = None,
                 fit_kwargs: dict = None,
                 act_kwargs: dict = None,
                 in_dist=True, noisy=False):

        model_kwargs = model_kwargs or {}
        fit_kwargs = fit_kwargs or {}
        act_kwargs = act_kwargs or {}

        data = {"n": [], "latent_dim": [], "embed_dim": [], "timesteps": [], "loss": [], "cost": []}
        total = len(self._N) * len(self._L) * len(self._E) * len(self._T) * len(self._C) * self._reps
        with tqdm(total=total, position=0, leave=False) as pbar:
            for i in range(self._reps):
                challenge = None
                for n, latent_dim, embed_dim, timesteps, max_control_cost in itertools.product(self._N, self._L, self._E, self._T, self._C):
                    pbar.set_description(f"Rep {i + 1}/{self._reps}: {n=}, {latent_dim=}, {embed_dim=}, {timesteps=}, {max_control_cost=}")
                    if embed_dim < latent_dim:
                        continue
                    if challenge is None:
                        challenge = self._challenge_cls(latent_dim, embed_dim, **self._challenge_kwargs)
                    if latent_dim != challenge.latent_dim:
                        challenge.latent_dim = latent_dim
                    if embed_dim != challenge.embed_dim:
                        challenge.embed_dim = embed_dim

                    # Create and train model
                    model = model_cls(embed_dim, timesteps, max_control_cost, **model_kwargs)
                    train_init_conds = challenge.make_init_conds(n)

                    total_control_cost = 0

                    for j in range(self._control_horizons):
                        if j == 0:
                            x = challenge.make_data(train_init_conds, timesteps=timesteps, noisy=noisy)
                        else:
                            control = model.act(x, **act_kwargs)
                            total_control_cost += challenge.calc_control_cost(control)
                            x = challenge.make_data(init_conds=x[:, 0], control=control, timesteps=timesteps,
                                                    noisy=noisy)
                        model.fit(x, **fit_kwargs)

                    assert total_control_cost <= max_control_cost, "Control cost exceeded!"

                    # create test data
                    test_init_conds = challenge.make_init_conds(self._test_examples, in_dist)
                    test = challenge.make_data(test_init_conds, timesteps=self._test_timesteps)
                    pred = model.predict(test[:, 0], timesteps)
                    loss = challenge.calc_loss(pred, test)
                    data["n"].append(n)
                    data["latent_dim"].append(latent_dim)
                    data["embed_dim"].append(embed_dim)
                    data["timesteps"].append(timesteps)
                    data["loss"].append(loss)
                    data["cost"].append(total_control_cost)
                    pbar.update()
                data["id"] = next(self._id)
        return pd.DataFrame(data)