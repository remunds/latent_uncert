import os
import torch

from torchvision.models.resnet import ResNet, BasicBlock, Bottleneck

# from net.resnet import ResNet, BasicBlock, Bottleneck
import torch.nn as nn
from spectral_normalization import spectral_norm
from simple_einet.einet import EinetConfig, Einet

from simple_einet.layers.distributions.normal import Normal
from tqdm import tqdm
import numpy as np
import torch.nn.functional as F


class EinetUtils:
    def start_train(
        self,
        dl_train,
        dl_valid,
        device,
        optimizer,
        lambda_v,
        warmup_epochs,
        num_epochs,
        deactivate_resnet=True,
        lr_schedule_resnet=None,
        lr_schedule_einet=None,
        early_stop=3,
        checkpoint_dir=None,
    ):
        self.train()
        val_increase = 0
        lowest_val_loss = torch.inf
        # warmup by only training resnet
        for epoch in range(warmup_epochs):
            loss = 0.0
            for data, target in tqdm(dl_train):
                optimizer.zero_grad()
                target = target.type(torch.LongTensor)
                data, target = data.to(device), target.to(device)
                output = self(data)
                loss_v = torch.nn.CrossEntropyLoss()(output, target)
                loss += loss_v.item()
                loss_v.backward()
                optimizer.step()
            if lr_schedule_resnet is not None:
                lr_schedule_resnet.step()

            val_loss = 0.0
            with torch.no_grad():
                for data, target in tqdm(dl_valid):
                    optimizer.zero_grad()
                    target = target.type(torch.LongTensor)
                    data, target = data.to(device), target.to(device)
                    output = self(data)
                    loss_v = torch.nn.CrossEntropyLoss()(output, target)
                    val_loss += loss_v.item()
            print(
                f"Epoch {epoch}, train loss {loss / len(dl_train.dataset)}, val loss {val_loss / len(dl_valid.dataset)}"
            )
            # early stopping
            if val_loss > lowest_val_loss:
                val_increase += 1
                if val_increase >= early_stop:
                    print(
                        f"Stopping Resnet early, val loss increased for the last {early_stop} epochs."
                    )
                    break
            else:
                val_increase = 0
                lowest_val_loss = val_loss
                if checkpoint_dir is not None:
                    self.save(checkpoint_dir + "checkpoint.pt")

        if checkpoint_dir is not None:
            # load best
            self.load(checkpoint_dir + "checkpoint.pt")

        # train einet (and optionally resnet jointly)
        self.activate_einet(deactivate_resnet)
        lowest_val_loss = torch.inf
        val_increase = 0
        for epoch in range(num_epochs):
            loss = 0.0
            for data, target in tqdm(dl_train):
                optimizer.zero_grad()
                target = target.type(torch.LongTensor)
                data, target = data.to(device), target.to(device)
                output = self(data)
                loss_v = (
                    lambda_v * torch.nn.CrossEntropyLoss()(output, target)
                    + (1 - lambda_v) * -output.mean()
                )
                loss += loss_v.item()
                loss_v.backward()
                optimizer.step()
            if lr_schedule_einet is not None:
                lr_schedule_einet.step()

            val_loss = 0.0
            with torch.no_grad():
                for data, target in tqdm(dl_valid):
                    optimizer.zero_grad()
                    target = target.type(torch.LongTensor)
                    data, target = data.to(device), target.to(device)
                    output = self(data)
                    loss_v = (
                        lambda_v * torch.nn.CrossEntropyLoss()(output, target)
                        + (1 - lambda_v) * -output.mean()
                    )
                    val_loss += loss_v.item()
                print(
                    f"Epoch {epoch}, train loss {loss / len(dl_train.dataset)}, val loss {val_loss / len(dl_valid.dataset)}"
                )
                # early stopping
                if val_loss > lowest_val_loss:
                    val_increase += 1
                    if val_increase >= early_stop:
                        print(
                            f"Stopping Einet early, val loss increased for the last {early_stop} epochs."
                        )
                        break
                else:
                    val_increase = 0
                    lowest_val_loss = val_loss
                    if checkpoint_dir is not None:
                        self.save(checkpoint_dir + "checkpoint.pt")
        if checkpoint_dir is not None:
            # load best
            self.load(checkpoint_dir + "checkpoint.pt")

    def save(self, path):
        # create path if not exists
        os.makedirs(os.path.dirname(path), exist_ok=True)
        torch.save(self.state_dict(), path)

    def load(self, path, resnet_only=False):
        self.load_state_dict(torch.load(path))
        if resnet_only:
            # reinitialize einet
            for param in self.einet.parameters():
                torch.nn.init.normal_(param, mean=0.0, std=0.01)

    def eval_acc(self, dl, device):
        self.eval()
        correct = 0
        total = 0
        with torch.no_grad():
            for data, labels in dl:
                data = data.to(device)
                labels = labels.to(device)
                total += labels.size(0)
                pred = self(data)
                pred = torch.argmax(pred, dim=1)
                correct += (pred == labels).sum().item()
        return correct / total

    def eval_ll(self, dl, device, return_all=False):
        self.eval()
        index = 0
        lls = torch.zeros(len(dl.dataset))
        with torch.no_grad():
            for data, _ in dl:
                data = data.to(device)
                ll = self(data)
                lls[index : index + len(ll)] = ll.mean(dim=1)
                index += len(ll)
        if return_all:
            return lls
        return torch.mean(lls)

    def eval_pred_variance(self, dl, device, return_all=False):
        self.eval()
        index = 0
        pred_vars = torch.zeros(len(dl.dataset))
        with torch.no_grad():
            for data, _ in dl:
                data = data.to(device)
                pred_logit = self(data)
                pred = torch.softmax(pred_logit, dim=1)
                pred = torch.max(pred, dim=1)[0]
                pred_var = pred * (1 - pred)
                pred_vars[index : index + len(pred_var)] = pred_var
                index += len(pred_var)
        if return_all:
            return pred_vars
        return torch.mean(pred_vars)

    def eval_pred_entropy(self, dl, device, return_all=False):
        self.eval()
        index = 0
        pred_entropies = torch.zeros(len(dl.dataset))
        with torch.no_grad():
            for data, _ in dl:
                data = data.to(device)
                pred_logit = self(data)
                pred = torch.softmax(pred_logit, dim=1)
                pred_entropy = -torch.sum(pred * torch.log(pred), dim=1)
                pred_entropies[index : index + len(pred_entropy)] = pred_entropy
                index += len(pred_entropy)
        if return_all:
            return pred_entropies
        return torch.mean(pred_entropies)

    def eval_dempster_shafer(self, dl, device, return_all=False):
        """
        SNGP: better for distance aware models, where
        magnitude of logits reflects distance from observed data manifold
        """
        num_classes = self.einet.config.num_classes
        self.eval()
        total = 0
        uncertainties = []
        with torch.no_grad():
            for data in dl:
                data = data.to(device)
                total += data.size(0)
                lls = self(data)
                # assumes negative log likelihoods
                # not really sure if normalizing even makes sense
                # but without, values are always 1(-) or 0(+)
                # lls_norm = lls / torch.min(lls, dim=1)[0].unsqueeze(1)
                lls_norm = lls - torch.mean(lls, dim=1).unsqueeze(1)
                lls_norm = lls_norm / torch.max(torch.abs(lls_norm), dim=1)[
                    0
                ].unsqueeze(1)
                uncertainty = num_classes / (
                    num_classes + torch.sum(torch.exp(lls_norm), dim=1)
                )
                uncertainties.append(uncertainty)
        if return_all:
            return uncertainties
        return torch.mean(uncertainties)

    def explain_ll(self, dl, device):
        """
        Check each explaining variable individually.
        Returns the difference in log likelihood between the default model and the marginalized model.
        If largely positive, the variable explains the result.
        """
        ll_default = self.eval_ll(dl, device)
        explanations = []
        for i in self.explaining_vars:
            self.marginalized_scopes = [i]
            ll_marg = self.eval_ll(dl, device)
            explanations.append(ll_default - ll_marg)
        self.marginalized_scopes = None
        return explanations

    def explain_mpe(self, dl, device):
        expl_var_vals = []
        expl_var_mpes = []
        for data, _ in dl:
            data = data.to(device)
            # extract explaining vars
            exp_vars = data[:, self.explaining_vars]
            expl_var_vals.append(exp_vars)
            # mask out explaining vars for resnet
            mask = torch.ones_like(data, dtype=torch.bool)
            mask[:, self.explaining_vars] = False
            data = data[mask]
            if self.image_shape is not None:
                # ConvResNetSPN
                # reshape to image
                data = data.reshape(
                    -1, self.image_shape[0], self.image_shape[1], self.image_shape[2]
                )
            else:
                # DenseResNetSPN
                data = data.reshape(-1, self.input_dim)

            # extract most probable explanation of current input
            hidden = self.forward_hidden(data)
            hidden = torch.cat([hidden, exp_vars], dim=1)
            mpe = self.einet.mpe(
                evidence=hidden, marginalized_scopes=self.explaining_vars
            )
            expl_var_mpes.append(mpe[:, self.explaining_vars])
        expl_var_vals = torch.cat(expl_var_vals, dim=0)
        expl_var_mpes = torch.cat(expl_var_mpes, dim=0)
        return torch.abs(expl_var_mpes - expl_var_vals).mean(dim=0).cpu().numpy()


class DenseResnet(nn.Module):
    """
    A simple fully connected ResNet.
    """

    def __init__(
        self,
        input_dim,
        output_dim,
        num_layers=3,
        num_hidden=128,
        dropout_rate=0.1,
        **classifier_kwargs,
    ):
        super(DenseResnet, self).__init__()
        # Defines class meta data.
        self.num_hidden = num_hidden
        self.num_layers = num_layers
        self.dropout_rate = dropout_rate
        self.classifier_kwargs = classifier_kwargs
        self.input_dim = input_dim
        self.output_dim = output_dim

        # Defines the hidden layers.
        self.input_layer = nn.Linear(self.input_dim, self.num_hidden)
        self.activation = nn.ReLU()
        self.dropout = nn.Dropout(self.dropout_rate)
        self.dense_layers = [self.make_dense_layer() for _ in range(num_layers)]
        self.dense_layers = nn.ModuleList(self.dense_layers)

        # Defines the output layer.
        self.classifier = self.make_output_layer()

    def forward(self, inputs):
        # Projects the 2d input data to high dimension.
        hidden = self.input_layer(inputs)

        # Computes the ResNet hidden representations.
        for i in range(self.num_layers):
            resid = self.dense_layers[i](hidden)
            resid = self.dropout(resid)
            hidden = hidden + resid

        return self.classifier(hidden)

    def make_dense_layer(self):
        """Uses the Dense layer as the hidden layer."""
        return nn.Sequential(
            nn.Linear(self.num_hidden, self.num_hidden), self.activation
        )

    def make_output_layer(self):
        """Uses the Dense layer as the output layer."""
        return nn.Linear(self.num_hidden, self.output_dim, **self.classifier_kwargs)


class DenseResNetSPN(DenseResnet, EinetUtils):
    """
    Spectral normalized ResNet with einet as the output layer.
    """

    def __init__(
        self, spec_norm_bound=0.9, explaining_vars=[], seperate_training=False, **kwargs
    ):
        self.spec_norm_bound = spec_norm_bound
        self.explaining_vars = explaining_vars
        super().__init__(**kwargs)
        self.einet = self.make_einet_output_layer()
        if seperate_training:
            # start with default resnet training
            # then train einet on hidden representations
            self.einet_active = False
            for param in self.einet.parameters():
                param.requires_grad = False
            # make sure to later set einet_active to True for training einet
        else:
            # train resnet and einet jointly end-to-end
            self.einet_active = True

    def activate_einet(self, deactivate_resnet=True):
        """
        Activates the einet output layer for second stage training and inference.
        """
        self.einet_active = True
        if deactivate_resnet:
            for param in self.parameters():
                param.requires_grad = False
        else:
            for param in self.parameters():
                param.requires_grad = True
        for param in self.einet.parameters():
            param.requires_grad = True

    def make_dense_layer(self):
        """applies spectral normalization to the hidden layer."""
        dense = nn.Linear(self.num_hidden, self.num_hidden)
        # todo: this is different to tf, since it does not use the spec_norm_bound...
        # note: both versions seem to work fine!
        # return nn.Sequential(
        #     nn.utils.parametrizations.spectral_norm(dense), self.activation
        # )
        return nn.Sequential(
            spectral_norm(dense, norm_bound=self.spec_norm_bound), self.activation
        )

    def make_einet_output_layer(self):
        """uses einet as the output layer."""
        cfg = EinetConfig(
            num_features=self.num_hidden + len(self.explaining_vars),
            num_channels=1,
            depth=3,
            num_sums=20,
            num_leaves=20,
            num_repetitions=1,
            num_classes=self.output_dim,
            leaf_type=Normal,
            # leaf_kwargs={"total_count": 2**n_bits - 1},
            layer_type="einsum",
            dropout=0.0,
        )
        model = Einet(cfg)
        return model

    def forward_hidden(self, inputs):
        hidden = self.input_layer(inputs)

        # Computes the ResNet hidden representations.
        for i in range(self.num_layers):
            resid = self.dense_layers[i](hidden)
            resid = self.dropout(resid)
            hidden = hidden + resid

        return hidden

    def forward(self, inputs):
        # extract explaining vars
        exp_vars = inputs[:, self.explaining_vars]
        # mask out explaining vars for resnet
        mask = torch.ones_like(inputs, dtype=torch.bool)
        mask[:, self.explaining_vars] = False
        inputs = inputs[mask]
        inputs = inputs.reshape(-1, self.input_dim)
        # feed through resnet
        hidden = self.forward_hidden(inputs)

        if self.einet_active:
            # classifier is einet, so we need to concatenate the explaining vars
            hidden = torch.cat([hidden, exp_vars], dim=1)
            return self.einet(hidden)

        return self.classifier(hidden)


class ResidualBlockSN(BasicBlock):
    """
    Spectral normalized ResNet block.
    """

    def __init__(self, *args, spec_norm_bound=0.9, **kwargs):
        super(ResidualBlockSN, self).__init__(*args, **kwargs)
        # self.conv1 = nn.utils.spectral_norm(self.conv1)
        # self.conv2 = nn.utils.spectral_norm(self.conv2)
        self.conv1 = spectral_norm(self.conv1, norm_bound=spec_norm_bound)
        self.conv2 = spectral_norm(self.conv2, norm_bound=spec_norm_bound)


class BottleNeckSN(Bottleneck):
    """
    Spectral normalized ResNet block.
    """

    # def __init__(self, spec_norm_bound=0.9, *args, **kwargs):
    def __init__(self, *args, spec_norm_bound=0.9, **kwargs):
        super(BottleNeckSN, self).__init__(*args, **kwargs)
        # self.conv1 = nn.utils.spectral_norm(self.conv1)
        # self.conv2 = nn.utils.spectral_norm(self.conv2)
        # self.conv3 = nn.utils.spectral_norm(self.conv3)
        self.conv1 = spectral_norm(self.conv1, norm_bound=spec_norm_bound)
        self.conv2 = spectral_norm(self.conv2, norm_bound=spec_norm_bound)
        self.conv3 = spectral_norm(self.conv3, norm_bound=spec_norm_bound)


class ConvResNetSPN(ResNet, EinetUtils):
    """
    Spectral normalized convolutional ResNet with einet as the output layer.
    """

    def __init__(
        self,
        block,
        layers,
        num_classes,
        image_shape,  # (C, H, W)
        explaining_vars=[],  # indices of variables that should be explained
        spec_norm_bound=0.9,
        seperate_training=False,
        **kwargs,
    ):
        super(ConvResNetSPN, self).__init__(block, layers, num_classes, **kwargs)
        self.conv1 = nn.Conv2d(
            image_shape[0],
            64,  # self.inplanes,
            kernel_size=(7, 7),
            stride=(2, 2),
            padding=(3, 3),
            bias=False,
        )
        # self.conv1 = nn.utils.spectral_norm(self.conv1)
        self.conv1 = spectral_norm(self.conv1, norm_bound=spec_norm_bound)
        self.bn2 = nn.BatchNorm2d(512)  # new
        self.explaining_vars = explaining_vars
        self.image_shape = image_shape
        self.marginalized_scopes = None
        self.einet = self.make_einet_output_layer(
            512 * block.expansion + len(explaining_vars), num_classes
        )
        if seperate_training:
            # start with default resnet training
            # then train einet on hidden representations
            self.einet_active = False
            for param in self.einet.parameters():
                param.requires_grad = False
            # make sure to later set einet_active to True for training einet
        else:
            # train resnet and einet jointly end-to-end
            self.einet_active = True

    def activate_einet(self, deactivate_resnet=True):
        """
        Activates the einet output layer for second stage training and inference.
        """
        self.einet_active = True
        if deactivate_resnet:
            for param in self.parameters():
                param.requires_grad = False
        else:
            for param in self.parameters():
                param.requires_grad = True
        for param in self.einet.parameters():
            param.requires_grad = True

    def make_einet_output_layer(self, in_features, out_features):
        """Uses einet as the output layer."""
        cfg = EinetConfig(
            num_features=in_features,
            num_channels=1,
            depth=3,
            num_sums=20,
            num_leaves=20,
            num_repetitions=10,
            num_classes=out_features,
            leaf_type=Normal,
            layer_type="einsum",
            dropout=0.0,
        )
        model = Einet(cfg)
        return model

    def forward_hidden(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.avgpool(x)
        x = self.bn2(x)  # new
        x = torch.flatten(x, 1)
        return x

    def _forward_impl(self, x):
        # x is flattened
        # extract explaining vars
        exp_vars = x[:, self.explaining_vars]
        # mask out explaining vars for resnet
        mask = torch.ones_like(x, dtype=torch.bool)
        mask[:, self.explaining_vars] = False
        x = x[mask]
        # reshape to image
        x = x.reshape(-1, self.image_shape[0], self.image_shape[1], self.image_shape[2])

        # feed through resnet
        x = self.forward_hidden(x)

        if self.einet_active:
            # classifier is einet, so we need to concatenate the explaining vars
            x = torch.cat([x, exp_vars], dim=1)
            return self.einet(x, marginalized_scopes=self.marginalized_scopes)

        return self.fc(x)


from net.resnet import ResNet


class ConvResnetDDU(ResNet, EinetUtils):
    def __init__(
        self,
        block,
        num_blocks,
        num_classes=10,
        temp=1.0,
        spectral_normalization=True,
        mod=True,
        coeff=3,
        n_power_iterations=1,
        image_shape=(1, 28, 28),  # (C, H, W)
        explaining_vars=[],  # indices of variables that should be explained
        seperate_training=False,
        **kwargs,
    ):
        mnist = image_shape[0] == 1 and image_shape[1] == 28 and image_shape[2] == 28
        super(ConvResnetDDU, self).__init__(
            block,
            num_blocks,
            num_classes,
            temp,
            spectral_normalization,
            mod,
            coeff,
            n_power_iterations,
            mnist,
        )

        self.explaining_vars = explaining_vars
        self.image_shape = image_shape
        self.marginalized_scopes = None
        self.einet = self.make_einet_output_layer(
            512 * block.expansion + len(explaining_vars), num_classes
        )
        if seperate_training:
            # start with default resnet training
            # then train einet on hidden representations
            self.einet_active = False
            for param in self.einet.parameters():
                param.requires_grad = False
            # make sure to later set einet_active to True for training einet
        else:
            # train resnet and einet jointly end-to-end
            self.einet_active = True

    def activate_einet(self, deactivate_resnet=True):
        """
        Activates the einet output layer for second stage training and inference.
        """
        self.einet_active = True
        if deactivate_resnet:
            for param in self.parameters():
                param.requires_grad = False
        else:
            for param in self.parameters():
                param.requires_grad = True
        for param in self.einet.parameters():
            param.requires_grad = True

    def make_einet_output_layer(self, in_features, out_features):
        """Uses einet as the output layer."""
        cfg = EinetConfig(
            num_features=in_features,
            num_channels=1,
            depth=3,
            num_sums=20,
            num_leaves=20,
            num_repetitions=10,
            num_classes=out_features,
            leaf_type=Normal,
            layer_type="einsum",
            dropout=0.0,
        )
        model = Einet(cfg)
        return model

    def forward_hidden(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.activation(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = F.avg_pool2d(x, 4)
        x = x.view(x.size(0), -1)
        return x

    def forward(self, x):
        # x is flattened
        # extract explaining vars
        exp_vars = x[:, self.explaining_vars]
        # mask out explaining vars for resnet
        mask = torch.ones_like(x, dtype=torch.bool)
        mask[:, self.explaining_vars] = False
        x = x[mask]
        # reshape to image
        x = x.reshape(-1, self.image_shape[0], self.image_shape[1], self.image_shape[2])

        # feed through resnet
        x = self.forward_hidden(x)

        if self.einet_active:
            # classifier is einet, so we need to concatenate the explaining vars
            x = torch.cat([x, exp_vars], dim=1)
            return self.einet(x, marginalized_scopes=self.marginalized_scopes)

        return self.fc(x) / self.temp