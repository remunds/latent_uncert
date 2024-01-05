import torch
from torch.utils.data import DataLoader
from torchvision import datasets, transforms
import os
import ddu_dirty_mnist
import mlflow


def get_datasets():
    data_dir = "/data_docker/datasets/"
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    mnist_transform = transforms.Compose(
        [
            # transforms.ToTensor(),
            transforms.Normalize((0.1307,), (0.3081,)),
            transforms.Lambda(lambda x: x.reshape(-1, 28 * 28).squeeze()),
        ]
    )
    fashion_mnist_transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize((0.1307,), (0.3081,)),
            transforms.Lambda(lambda x: x.reshape(-1, 28 * 28).squeeze()),
        ]
    )

    # load dirty mnist
    train_ds = ddu_dirty_mnist.DirtyMNIST(
        data_dir + "dirty_mnist",
        train=True,
        transform=mnist_transform,
        download=True,
        normalize=False,
        device=device,
    )
    test_ds = ddu_dirty_mnist.DirtyMNIST(
        data_dir + "dirty_mnist",
        train=False,
        transform=mnist_transform,
        download=True,
        normalize=False,
        device=device,
    )

    ambiguous_ds_test = ddu_dirty_mnist.AmbiguousMNIST(
        data_dir + "dirty_mnist",
        train=False,
        transform=mnist_transform,
        download=True,
        normalize=False,
        device=device,
    )

    mnist_ds_test = ddu_dirty_mnist.FastMNIST(
        data_dir + "dirty_mnist",
        train=False,
        transform=mnist_transform,
        download=True,
        normalize=False,
        device=device,
    )

    ood_ds = datasets.FashionMNIST(
        data_dir + "fashionmnist",
        train=False,
        download=True,
        transform=fashion_mnist_transform,
    )

    train_ds, valid_ds = torch.utils.data.random_split(
        train_ds, [0.8, 0.2], generator=torch.Generator().manual_seed(0)
    )

    return train_ds, valid_ds, test_ds, ambiguous_ds_test, mnist_ds_test, ood_ds


def start_dirty_mnist_run(run_name, batch_sizes, model_params, train_params, trial):
    with mlflow.start_run(run_name=run_name) as run:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        mlflow.log_param("device", device)

        ckpt_dir = f"/data_docker/ckpts/dirty_mnist/{run_name}/"
        os.makedirs(ckpt_dir, exist_ok=True)
        mlflow.log_param("ckpt_dir", ckpt_dir)

        # log all params
        mlflow.log_params(batch_sizes)
        mlflow.log_params(model_params)
        mlflow.log_params(train_params)

        # load data
        (
            train_ds,
            valid_ds,
            test_ds,
            ambiguous_ds_test,
            mnist_ds_test,
            ood_ds,
        ) = get_datasets()

        # create dataloaders
        train_dl = DataLoader(
            train_ds,
            batch_size=batch_sizes["resnet"],
            shuffle=True,
            pin_memory=False,
            num_workers=0,
        )
        valid_dl = DataLoader(
            valid_ds,
            batch_size=batch_sizes["resnet"],
            shuffle=True,
            pin_memory=False,
            num_workers=0,
        )
        test_dl = DataLoader(
            test_ds,
            batch_size=batch_sizes["resnet"],
            shuffle=True,
            pin_memory=False,
            num_workers=0,
        )

        # create model
        model_name = model_params["model"]
        del model_params["model"]
        if model_name == "ConvResNetSPN":
            from ResNetSPN import ConvResNetSPN, ResidualBlockSN, BottleNeckSN

            if model_params["block"] == "basic":
                block = ResidualBlockSN
            elif model_params["block"] == "bottleneck":
                block = BottleNeckSN
            else:
                raise NotImplementedError

            del model_params["block"]
            layers = model_params["layers"]
            del model_params["layers"]
            del model_params["spectral_normalization"]
            del model_params["mod"]

            model = ConvResNetSPN(
                block,
                layers,
                explaining_vars=[],  # for calibration test, we don't need explaining vars
                **model_params,
            )
        elif model_name == "ConvResNetDDU":
            from ResNetSPN import ConvResnetDDU
            from net.resnet import BasicBlock, Bottleneck

            if model_params["block"] == "basic":
                block = BasicBlock
            elif model_params["block"] == "bottleneck":
                block = Bottleneck
            else:
                raise NotImplementedError

            del model_params["block"]
            layers = model_params["layers"]
            del model_params["layers"]
            del model_params["spec_norm_bound"]
            model = ConvResnetDDU(
                block,
                layers,
                explaining_vars=[],  # for calibration test, we don't need explaining vars
                **model_params,
            )
        elif model_name == "AutoEncoderSPN":
            from ResNetSPN import AutoEncoderSPN

            model = AutoEncoderSPN(
                explaining_vars=[],  # for calibration test, we don't need explaining vars
                **model_params,
            )
        elif model_name == "EfficientNetSPN":
            from ResNetSPN import EfficientNetSPN

            model = EfficientNetSPN(
                explaining_vars=[],  # for calibration test, we don't need explaining vars
                **model_params,
            )
        else:
            raise NotImplementedError
        mlflow.set_tag("model", model.__class__.__name__)
        model = model.to(device)

        print("training model")
        # train model
        lowest_val_loss = model.start_train(
            train_dl,
            valid_dl,
            device,
            checkpoint_dir=ckpt_dir,
            trial=trial,
            **train_params,
        )
        mlflow.pytorch.log_state_dict(model.state_dict(), "model")

        if train_params["num_epochs"] == 0:
            return lowest_val_loss

        # evaluate
        model.eval()
        model.einet_active = False
        train_acc = model.eval_acc(train_dl, device)
        mlflow.log_metric("train accuracy resnet", train_acc)
        test_acc = model.eval_acc(test_dl, device)
        mlflow.log_metric("test accuracy resnet", test_acc)

        model.einet_active = True
        train_acc = model.eval_acc(train_dl, device)
        mlflow.log_metric("train accuracy", train_acc)
        test_acc = model.eval_acc(test_dl, device)
        mlflow.log_metric("test accuracy", test_acc)

        train_ll = model.eval_ll(train_dl, device)
        mlflow.log_metric("train ll", train_ll)
        test_ll = model.eval_ll(test_dl, device)
        mlflow.log_metric("test ll", test_ll)

        # create dataloaders
        mnist_dl = DataLoader(
            mnist_ds_test,
            batch_size=batch_sizes["resnet"],
            shuffle=False,
            pin_memory=False,
            num_workers=0,
        )
        ambiguous_dl = DataLoader(
            ambiguous_ds_test,
            batch_size=batch_sizes["resnet"],
            shuffle=False,
            pin_memory=False,
            num_workers=0,
        )
        ood_dl = DataLoader(
            ood_ds,
            batch_size=batch_sizes["resnet"],
            shuffle=False,
            pin_memory=False,
            num_workers=0,
        )

        # plot as in DDU paper
        # three datasets: mnist, ambiguous mnist, ood
        # x-axis: log-likelihood, y-axis: fraction of data

        # get log-likelihoods for all datasets
        with torch.no_grad():
            lls_mnist = model.eval_ll(mnist_dl, device, return_all=True)
            mlflow.log_metric("mnist ll", torch.mean(lls_mnist).item())
            pred_var_mnist = model.eval_pred_variance(mnist_dl, device, return_all=True)
            mlflow.log_metric("mnist pred var", torch.mean(pred_var_mnist).item())
            pred_entropy_mnist = model.eval_pred_entropy(
                mnist_dl, device, return_all=True
            )
            mlflow.log_metric(
                "mnist pred entropy", torch.mean(pred_entropy_mnist).item()
            )

            lls_amb = model.eval_ll(ambiguous_dl, device, return_all=True)
            mlflow.log_metric("ambiguous ll", torch.mean(lls_amb).item())
            pred_var_amb = model.eval_pred_variance(
                ambiguous_dl, device, return_all=True
            )
            mlflow.log_metric("ambiguous pred var", torch.mean(pred_var_amb).item())
            pred_entropy_amb = model.eval_pred_entropy(
                ambiguous_dl, device, return_all=True
            )
            mlflow.log_metric(
                "ambiguous pred entropy", torch.mean(pred_entropy_amb).item()
            )

            lls_ood = model.eval_ll(ood_dl, device, return_all=True)
            mlflow.log_metric("ood ll", torch.mean(lls_ood).item())
            pred_var_ood = model.eval_pred_variance(ood_dl, device, return_all=True)
            mlflow.log_metric("ood pred var", torch.mean(pred_var_ood).item())
            pred_entropy_ood = model.eval_pred_entropy(ood_dl, device, return_all=True)
            mlflow.log_metric("ood pred entropy", torch.mean(pred_entropy_ood).item())

        # plot
        def hist_plot(data_mnist, data_amb, data_ood, xlabel, ylabel, filename):
            import matplotlib.pyplot as plt
            import seaborn as sns

            sns.set_style("whitegrid")
            sns.set_context("paper", font_scale=1.5)
            # plot mnist
            fig, ax = plt.subplots()
            sns.histplot(
                data_mnist.cpu().numpy(),
                stat="probability",
                label="MNIST",
                ax=ax,
                bins=30,
                # binrange=(min, max),
            )
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)

            # plot ambiguous mnist
            sns.histplot(
                data_amb.cpu().numpy(),
                stat="probability",
                label="Ambiguous MNIST",
                ax=ax,
                bins=30,
                # binrange=(min, max),
            )

            # plot ood
            sns.histplot(
                data_ood.cpu().numpy(),
                stat="probability",
                label="Fashion MNIST",
                bins=30,
                ax=ax,
                # binrange=(min, max),
            )

            fig.legend()
            fig.tight_layout()
            mlflow.log_figure(fig, filename)
            plt.close()

        # Likelihood plot
        hist_plot(
            lls_mnist,
            lls_amb,
            lls_ood,
            "Log-likelihood",
            "Fraction of data",
            "mnist_amb_ood_ll.png",
        )

        # Predictive variance plot
        hist_plot(
            pred_var_mnist,
            pred_var_amb,
            pred_var_ood,
            "Predictive variance",
            "Fraction of data",
            "mnist_amb_ood_pred_var.png",
        )

        # Predictive entropy plot
        hist_plot(
            pred_entropy_mnist,
            pred_entropy_amb,
            pred_entropy_ood,
            "Predictive entropy",
            "Fraction of data",
            "mnist_amb_ood_pred_entropy.png",
        )

        return lowest_val_loss
