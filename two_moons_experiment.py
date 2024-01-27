import torch

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as colors
import sklearn.datasets

from torch.utils.data import DataLoader
import os
import mlflow

### data and stuff from here: https://www.tensorflow.org/tutorials/understanding/sngp
### visualization macros


def plot_uncertainty_surface(
    train_examples,
    train_labels,
    ood_examples,
    test_uncertainty,
    ax,
    cmap=None,
    plot_train=True,
    input_is_ll=False,
):
    """Visualizes the 2D uncertainty surface.

    For simplicity, assume these objects already exist in the memory:

        test_examples: Array of test examples, shape (num_test, 2).
        train_labels: Array of train labels, shape (num_train, ).
        train_examples: Array of train examples, shape (num_train, 2).

    Arguments:
        test_uncertainty: Array of uncertainty scores, shape (num_test,).
        ax: A matplotlib Axes object that specifies a matplotlib figure.
        cmap: A matplotlib colormap object specifying the palette of the
        predictive surface.

    Returns:
        pcm: A matplotlib PathCollection object that contains the palette
        information of the uncertainty plot.
    """
    plt.rcParams["figure.dpi"] = 140
    DEFAULT_X_RANGE = (-3.5, 3.5)
    DEFAULT_Y_RANGE = (-2.5, 2.5)
    DEFAULT_CMAP = colors.ListedColormap(["#377eb8", "#ff7f00"])
    DEFAULT_NORM = colors.Normalize(
        vmin=0,
        vmax=1,
    )
    DEFAULT_N_GRID = 100
    # Normalize uncertainty for better visualization.
    test_uncertainty = test_uncertainty - np.min(test_uncertainty)
    test_uncertainty = test_uncertainty / (
        np.max(test_uncertainty) - np.min(test_uncertainty)
    )

    # Set view limits.
    ax.set_ylim(DEFAULT_Y_RANGE)
    ax.set_xlim(DEFAULT_X_RANGE)

    # Plot normalized uncertainty surface.
    pcm = ax.imshow(
        np.reshape(test_uncertainty, [DEFAULT_N_GRID, DEFAULT_N_GRID]),
        cmap=cmap,
        origin="lower",
        extent=DEFAULT_X_RANGE + DEFAULT_Y_RANGE,
        vmin=DEFAULT_NORM.vmin,
        vmax=DEFAULT_NORM.vmax,
        interpolation="bicubic",
        aspect="auto",
    )

    if plot_train:
        # Plot training data.
        ax.scatter(
            train_examples[:, 0],
            train_examples[:, 1],
            c=train_labels,
            cmap=DEFAULT_CMAP,
            alpha=0.5,
        )
        ax.scatter(ood_examples[:, 0], ood_examples[:, 1], c="red", alpha=0.1)

    return pcm


def make_training_data(sample_size=500):
    """Create two moon training dataset."""
    train_examples, train_labels = sklearn.datasets.make_moons(
        n_samples=2 * sample_size, noise=0.1
    )

    # Adjust data position slightly.
    train_examples[train_labels == 0] += [-0.1, 0.2]
    train_examples[train_labels == 1] += [0.1, -0.2]

    return train_examples.astype(np.float32), train_labels.astype(np.int32)


def make_testing_data():
    """Create a mesh grid in 2D space."""
    plt.rcParams["figure.dpi"] = 140
    DEFAULT_X_RANGE = (-3.5, 3.5)
    DEFAULT_Y_RANGE = (-2.5, 2.5)
    DEFAULT_N_GRID = 100
    x_range = DEFAULT_X_RANGE
    y_range = DEFAULT_Y_RANGE
    n_grid = DEFAULT_N_GRID
    # testing data (mesh grid over data space)
    x = np.linspace(x_range[0], x_range[1], n_grid).astype(np.float32)
    y = np.linspace(y_range[0], y_range[1], n_grid).astype(np.float32)
    xv, yv = np.meshgrid(x, y)
    return np.stack([xv.flatten(), yv.flatten()], axis=-1)


def make_ood_data(sample_size=500, means=(2.5, -1.75), vars=(0.01, 0.01)):
    return np.random.multivariate_normal(
        means, cov=np.diag(vars), size=sample_size
    ).astype(np.float32)


def start_two_moons_run(run_name, batch_sizes, model_params, train_params, trial):
    print("starting new two-moons run: ", run_name)
    with mlflow.start_run(run_name=run_name):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        mlflow.log_param("device", device)

        ckpt_dir = f"ckpts/two_moons/{run_name}/"
        os.makedirs(ckpt_dir, exist_ok=True)
        mlflow.log_param("ckpt_dir", ckpt_dir)

        # log all params
        mlflow.log_params(batch_sizes)
        mlflow.log_params(model_params)
        mlflow.log_params(train_params)

        # Load the train, test and OOD datasets.
        train_examples, train_labels = make_training_data(sample_size=500)
        test_examples = make_testing_data()
        ood_examples = make_ood_data(sample_size=500)

        pos_examples = train_examples[train_labels == 0]
        neg_examples = train_examples[train_labels == 1]

        # put into data loaders
        train_ds = list(zip(train_examples, train_labels))
        train_dl = DataLoader(
            train_ds[:900],
            batch_size=batch_sizes["resnet"],
            shuffle=True,
            pin_memory=True,
            num_workers=1,
        )
        valid_dl = DataLoader(
            train_ds[900:],
            batch_size=batch_sizes["resnet"],
            shuffle=True,
            pin_memory=True,
            num_workers=1,
        )

        from ResNetSPN import DenseResNetSPN

        resnet_spn = DenseResNetSPN(**model_params)
        mlflow.set_tag("model", resnet_spn.__class__.__name__)
        print(resnet_spn)
        resnet_spn.to(device)
        # it is interesting to play with lambda_v, dropout, repetition and depth
        lowest_val_loss = resnet_spn.start_train(
            train_dl,
            valid_dl,
            device,
            checkpoint_dir=ckpt_dir,
            trial=trial,
            **train_params,
        )
        mlflow.pytorch.log_state_dict(resnet_spn.state_dict(), "model")

        if train_params["num_epochs"] == 0:
            return lowest_val_loss
        # evaluate
        resnet_spn.eval()
        valid_acc = resnet_spn.eval_acc(valid_dl, device)
        print("valid accuracy: ", valid_acc)
        mlflow.log_metric("valid accuracy", valid_acc)

        pos_dl = DataLoader(
            pos_examples,
            batch_size=batch_sizes["resnet"],
            pin_memory=False,
            num_workers=1,
        )
        # get LL's
        pos_ll = resnet_spn.eval_ll(pos_dl, device)
        mlflow.log_metric("pos_ll", pos_ll)
        neg_dl = DataLoader(
            neg_examples,
            batch_size=batch_sizes["resnet"],
            pin_memory=False,
            num_workers=1,
        )
        neg_ll = resnet_spn.eval_ll(neg_dl, device)
        mlflow.log_metric("neg_ll", neg_ll)
        ood_dl = DataLoader(
            ood_examples,
            batch_size=batch_sizes["resnet"],
            pin_memory=False,
            num_workers=1,
        )
        ood_ll = resnet_spn.eval_ll(ood_dl, device)
        mlflow.log_metric("ood_ll", ood_ll)

        del pos_dl, neg_dl, ood_dl

        test_dl = DataLoader(
            test_examples,
            batch_size=batch_sizes["resnet"],
            pin_memory=True,
            num_workers=1,
        )

        # Visualize SPN posterior
        posteriors = resnet_spn.eval_posterior(test_dl, device, return_all=True)
        posteriors = torch.exp(posteriors)
        # take the probability of class 0 as the uncertainty
        # if p==1 -> no uncertainty, if p==0 -> high uncertainty
        probs_class_0 = posteriors[:, 0].cpu().detach().numpy()
        fig, ax = plt.subplots(figsize=(7, 5.5))
        pcm = plot_uncertainty_surface(
            train_examples, train_labels, ood_examples, probs_class_0, ax=ax
        )
        plt.colorbar(pcm, ax=ax)
        plt.title("Class Probability, SPN model")
        mlflow.log_figure(fig, "posterior_class_probability.png")

        # Visualize SPN predictive entropy
        entropy = -torch.sum(posteriors * torch.log(posteriors), axis=1)
        entropy = entropy.cpu().detach().numpy()
        fig, ax = plt.subplots(figsize=(7, 5.5))
        pcm = plot_uncertainty_surface(
            train_examples, train_labels, ood_examples, entropy, ax=ax
        )
        plt.colorbar(pcm, ax=ax)
        plt.title("Predictive Entropy, SPN Model")
        mlflow.log_figure(fig, "posterior_predictive_entropy.png")

        # Visualize SPN predictive variance/uncertainty
        # unnecessary according to fabrizio, because we have the entropy
        variance = probs_class_0 * (1 - probs_class_0)
        fig, ax = plt.subplots(figsize=(7, 5.5))
        pcm = plot_uncertainty_surface(
            train_examples, train_labels, ood_examples, variance, ax=ax
        )
        plt.colorbar(pcm, ax=ax)
        plt.title("Predictive Variance, SPN Model")
        mlflow.log_figure(fig, "posterior_predictive_variance.png")

        lls = resnet_spn.eval_ll(test_dl, device, return_all=True)
        nll = -(lls.cpu().detach().numpy())  # negative log likelihood
        fig, ax = plt.subplots(figsize=(7, 5.5))
        pcm = plot_uncertainty_surface(
            train_examples, train_labels, ood_examples, nll, ax=ax
        )
        plt.colorbar(pcm, ax=ax)
        plt.title("NLL, SPN Model")
        mlflow.log_figure(fig, "nll.png")

        fig, ax = plt.subplots(figsize=(7, 5.5))
        pcm = plot_uncertainty_surface(
            train_examples,
            train_labels,
            ood_examples,
            nll,
            ax=ax,
            plot_train=False,
        )
        plt.colorbar(pcm, ax=ax)
        plt.title("NLL, SPN Model")
        mlflow.log_figure(fig, "nll_no_train.png")

        dempster_shafer = (
            resnet_spn.eval_dempster_shafer(test_dl, device, return_all=True)
            .cpu()
            .detach()
            .numpy()
        )
        fig, ax = plt.subplots(figsize=(7, 5.5))
        pcm = plot_uncertainty_surface(
            train_examples, train_labels, ood_examples, dempster_shafer, ax=ax
        )
        plt.colorbar(pcm, ax=ax)
        plt.title("Dempster Shafer, SPN Model")
        mlflow.log_figure(fig, "dempster_shafer.png")

        # maximum predictive probability as in Figure 3, appendix C in https://arxiv.org/pdf/2006.10108.pdf
        max_pred_prob = np.max(posteriors.cpu().detach().numpy(), axis=1)
        uncertainty = 1 - 2 * np.abs(max_pred_prob - 0.5)
        fig, ax = plt.subplots(figsize=(7, 5.5))
        pcm = plot_uncertainty_surface(
            train_examples, train_labels, ood_examples, uncertainty, ax=ax
        )
        plt.colorbar(pcm, ax=ax)
        plt.title("Maximum Predictive Probability, SPN Model")
        mlflow.log_figure(fig, "max_pred_prob.png")

        plt.close()
        return lowest_val_loss
