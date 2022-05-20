"""Tests with Pandas."""
import warnings

import numpy
import pandas
import pytest
from concrete.numpy import MAXIMUM_BIT_WIDTH
from sklearn.exceptions import ConvergenceWarning
from torch import nn

from concrete.ml.sklearn import (
    DecisionTreeClassifier,
    GammaRegressor,
    LinearRegression,
    LinearSVC,
    LinearSVR,
    LogisticRegression,
    NeuralNetClassifier,
    PoissonRegressor,
    RandomForestClassifier,
    TweedieRegressor,
    XGBClassifier,
)

regression_models = [
    GammaRegressor,
    LinearRegression,
    LinearSVR,
    PoissonRegressor,
    TweedieRegressor,
]

classifier_models = [
    DecisionTreeClassifier,
    RandomForestClassifier,
    XGBClassifier,
    LinearSVC,
    LogisticRegression,
]

classifiers = [
    pytest.param(
        model,
        {
            "dataset": "classification",
            "n_samples": 1000,
            "n_features": 100,
            "n_classes": n_classes,
            "n_informative": 100,
            "n_redundant": 0,
        },
        id=f"{model.__name__}_n_classes_{n_classes}",
    )
    for model in classifier_models
    for n_classes in [2, 4]
]

# Only LinearRegression supports multi targets
# GammaRegressor, PoissonRegressor and TweedieRegressor only handle positive target values
regressors = [
    pytest.param(
        model,
        {
            "dataset": "regression",
            "strictly_positive": model in [GammaRegressor, PoissonRegressor, TweedieRegressor],
            "n_samples": 200,
            "n_features": 10,
            "n_informative": 10,
            "n_targets": 2 if model == LinearRegression else 1,
            "noise": 0,
        },
        id=model.__name__,
    )
    for model in regression_models
]


@pytest.mark.parametrize("model, parameters", classifiers + regressors)
def test_pandas(model, parameters, load_data):
    """Tests that calling fit multiple times gives the same results"""

    # FIXME: LinearRegression problem
    if model.__name__ == "LinearRegression":
        return

    x, y = load_data(random_state=numpy.random.randint(0, 2**15), **parameters)

    # Turn to Pandas
    x = pandas.DataFrame(x)
    y = pandas.Series(y)

    model = model(n_bits=2)

    # Some models use a bit of randomness while fitting under scikit-learn, making the
    # outputs always different after each fit. In order to avoid that problem, their random_state
    # parameter needs to be fixed each time the test is ran.
    model_params = model.get_params()
    if "random_state" in model_params:
        model_params["random_state"] = numpy.random.randint(0, 2**15)
    model.set_params(**model_params)

    # Sometimes, we miss convergence, which is not a problem for our test
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ConvergenceWarning)

        model.fit(x, y)
        model.predict(x.to_numpy())


def test_pandas_qnn(load_data):
    """Tests with pandas"""

    x, y = load_data(
        dataset="classification",
        n_samples=1000,
        n_features=10,
        n_redundant=0,
        n_repeated=0,
        n_informative=5,
        n_classes=2,
        class_sep=2,
        random_state=42,
    )
    x = x.astype(numpy.float32)

    # Turn to Pandas
    x = pandas.DataFrame(x)
    y = pandas.Series(y)

    params = {
        "module__n_layers": 3,
        "module__n_w_bits": 2,
        "module__n_a_bits": 2,
        "module__n_accum_bits": MAXIMUM_BIT_WIDTH,
        "module__n_outputs": 2,
        "module__input_dim": 10,
        "module__activation_function": nn.SELU,
        "max_epochs": 10,
        "verbose": 0,
    }

    model = NeuralNetClassifier(**params)

    # Some models use a bit of randomness while fitting under scikit-learn, making the
    # outputs always different after each fit. In order to avoid that problem, their random_state
    # parameter needs to be fixed each time the test is ran.
    model_params = model.get_params()
    if "random_state" in model_params:
        model_params["random_state"] = numpy.random.randint(0, 2**15)
    model.set_params(**model_params)

    model.fit(x, y)
    model.predict(x.to_numpy())
