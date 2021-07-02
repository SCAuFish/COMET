import abc
import multiprocessing
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pytorch_lightning as ptl
import torch
from comet.encoders import str2encoder
from comet.modules import LayerwiseAttention
from torch import nn
from torch.utils.data import DataLoader, RandomSampler, Subset

from .utils import average_pooling, max_pooling


class CometModel(ptl.LightningModule, metaclass=abc.ABCMeta):
    def __init__(
        self,
        nr_frozen_epochs: int = 0.4,
        keep_embeddings_frozen: bool = False,
        optimizer: str = "AdamW",
        encoder_learning_rate: float = 1e-05,
        learning_rate: float = 3e-05,
        layerwise_decay: float = 0.95,
        encoder_model: str = "XLM-RoBERTa",
        pretrained_model: str = "xlm-roberta-large",
        pool: str = "avg",
        layer: Union[str, int] = "mix",
        dropout: float = 0.1,
        batch_size: int = 8,
        train_data: Optional[str] = None,
        validation_data: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.save_hyperparameters()
        self.encoder = str2encoder[self.hparams.encoder_model].from_pretrained(
            self.hparams.pretrained_model
        )

        if self.hparams.layer == "mix":
            self.layerwise_attention = LayerwiseAttention(
                num_layers=self.encoder.num_layers,
                dropout=self.hparams.dropout,
                layer_norm=True,
            )
        else:
            self.layerwise_attention = None

        if self.hparams.nr_frozen_epochs > 0:
            self._frozen = True
            self.freeze_encoder()
        else:
            self._frozen = False

        if self.hparams.keep_embeddings_frozen:
            self.encoder.freeze_embeddings()

        self.nr_frozen_epochs = self.hparams.nr_frozen_epochs

    @abc.abstractmethod
    def read_csv(self):
        pass

    @abc.abstractmethod
    def prepare_sample(
        self, sample: List[Dict[str, Union[str, float]]], *args, **kwargs
    ):
        pass

    @abc.abstractmethod
    def configure_optimizers(self):
        pass

    @abc.abstractmethod
    def init_metrics(self) -> None:
        pass

    @abc.abstractmethod
    def forward(self, *args, **kwargs) -> Dict[str, torch.Tensor]:
        pass

    def freeze_encoder(self) -> None:
        """Freezes the encoder layer."""
        self.encoder.freeze()

    @property
    def loss(self):
        return nn.MSELoss(reduction="sum")

    def compute_loss(self, predictions, targets):
        return self.loss(predictions["score"].view(-1), targets["score"])

    def unfreeze_encoder(self) -> None:
        """un-freezes the encoder layer."""
        if self._frozen:
            if self.trainer.is_global_zero:
                print("\nEncoder model fine-tuning")

            self.encoder.unfreeze()
            self._frozen = False
            if self.hparams.keep_embeddings_frozen:
                self.encoder.freeze_embeddings()

    def on_epoch_end(self):
        """Hook used to unfreeze encoder during training."""
        if self.current_epoch >= self.nr_frozen_epochs and self._frozen:
            self.unfreeze_encoder()
            self._frozen = False

    def get_sentence_embedding(
        self, input_ids: torch.Tensor, attention_mask: torch.Tensor
    ) -> torch.Tensor:
        """Auxiliar function that extracts sentence embeddings for
            a single sentence.

        :param tokens: sequences [batch_size x seq_len]
        :param lengths: lengths [batch_size]

        :return: torch.Tensor [batch_size x hidden_size]
        """
        encoder_out = self.encoder(input_ids, attention_mask)
        if self.layerwise_attention:
            embeddings = self.layerwise_attention(
                encoder_out["all_layers"], attention_mask
            )

        elif self.hparams.layer >= 0 and self.hparams.layer < self.encoder.num_layers:
            embeddings = encoder_out["all_layers"][self.hparams.layer]

        else:
            raise Exception("Invalid model layer {}.".format(self.hparams.layer))

        if self.hparams.pool == "default":
            sentemb = encoder_out["sentemb"]

        elif self.hparams.pool == "max":
            sentemb = max_pooling(
                input_ids, embeddings, self.encoder.tokenizer.pad_token_id
            )

        elif self.hparams.pool == "avg":
            sentemb = average_pooling(
                input_ids,
                embeddings,
                attention_mask,
                self.encoder.tokenizer.pad_token_id,
            )

        elif self.hparams.pool == "cls":
            sentemb = embeddings[:, 0, :]

        else:
            raise Exception("Invalid pooling technique.")

        return sentemb

    def training_step(
        self,
        batch: Tuple[Dict[str, torch.Tensor], Dict[str, torch.Tensor]],
        batch_nb: int,
    ) -> Dict[str, torch.Tensor]:
        """
        Runs one training step.
        This usually consists in the forward function followed by the loss function.

        :param batch: The output of your prepare_sample function.
        :param batch_nb: Integer displaying which batch this is.

        :returns: dictionary containing the loss and the metrics to be added to the lightning logger.
        """
        batch_input, batch_target = batch
        batch_prediction = self.forward(**batch_input)
        loss_value = self.compute_loss(batch_prediction, batch_target)

        if (
            self.nr_frozen_epochs < 1.0
            and self.nr_frozen_epochs > 0.0
            and batch_nb > self.epoch_total_steps * self.nr_frozen_epochs
        ):
            self.unfreeze_encoder()
            self._frozen = False

        self.log("train_loss", loss_value, on_step=True, on_epoch=True)
        return loss_value

    def validation_step(
        self,
        batch: Tuple[Dict[str, torch.Tensor], Dict[str, torch.Tensor]],
        batch_nb: int,
        dataloader_idx: int,
    ) -> Dict[str, torch.Tensor]:
        """
        Similar to the training step but with the model in eval mode.

        :param batch: The output of your prepare_sample function.
        :param batch_nb: Integer displaying which batch this is.
        :param dataloader_idx: Integer displaying which dataloader this is.

        :returns: dictionary passed to the validation_end function.
        """
        batch_input, batch_target = batch
        batch_prediction = self.forward(**batch_input)
        loss_value = self.compute_loss(batch_prediction, batch_target)
        
        self.log("val_loss", loss_value, on_step=True, on_epoch=True)

        # TODO: REMOVE if condition after torchmetrics bug fix
        if batch_prediction["score"].view(-1).size() != torch.Size([1]):
            if dataloader_idx == 0:
                self.train_metrics.update(
                    batch_prediction["score"].view(-1), batch_target["score"]
                )
            elif dataloader_idx == 1:
                self.val_metrics.update(
                    batch_prediction["score"].view(-1), batch_target["score"]
                )

    def predict_step(self, batch: Dict[str, torch.Tensor], batch_idx: int, dataloader_idx: Optional[int]) -> List[float]:
        return self(**batch)["score"].view(-1)

    def validation_epoch_end(self, *args, **kwargs) -> None:
        self.log_dict(self.train_metrics.compute(), prog_bar=True)
        self.log_dict(self.val_metrics.compute(), prog_bar=True)
        self.train_metrics.reset()
        self.val_metrics.reset()

    def setup(self, stage) -> None:
        """Data preparation function called before training by Lightning.
        Equivalent to the prepare_data in previous Lightning Versions"""
        self.train_dataset = self.read_csv(self.hparams.train_data)
        self.validation_dataset = self.read_csv(self.hparams.validation_data)


        self.epoch_total_steps = len(self.train_dataset) // (
            self.hparams.batch_size * max(1, self.trainer.num_gpus)
        )
        self.total_steps = self.epoch_total_steps * float(self.trainer.max_epochs)
        
        # Always validate the model with 2k examples from training to control overfit.
        train_subset = np.random.choice(a=len(self.train_dataset), size=5)
        self.train_subset = Subset(self.train_dataset, train_subset)
        self.init_metrics()

    def train_dataloader(self) -> DataLoader:
        """Function that loads the train set."""
        return DataLoader(
            dataset=self.train_dataset,
            sampler=RandomSampler(self.train_dataset),
            batch_size=self.hparams.batch_size,
            collate_fn=self.prepare_sample,
            num_workers=multiprocessing.cpu_count(),
        )

    def val_dataloader(self) -> DataLoader:
        """Function that loads the validation set."""
        return [
            DataLoader(
                dataset=self.train_subset,
                batch_size=self.hparams.batch_size,
                collate_fn=self.prepare_sample,
                num_workers=multiprocessing.cpu_count(),
            ),
            DataLoader(
                dataset=self.validation_dataset,
                batch_size=self.hparams.batch_size,
                collate_fn=self.prepare_sample,
                num_workers=multiprocessing.cpu_count(),
            ),
        ]