import torch
import torch.nn as nn
import tensorflow as tf
import numpy as np
from tqdm import tqdm
import os
import logging
import csv
import random

logging.basicConfig(level=logging.INFO)
import argparse

from linear_models import weight_init, CoordConvEncoder
from utils import (
    load_dataset,
    evaluate,
    set_seed_everywhere,
)
from dopamine.discrete_domains.atari_lib import create_atari_environment
import kornia

gfile = tf.io.gfile


def train(args):
    device = torch.device("cuda")

    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    set_seed_everywhere(args.seed)

    ## fixed dataset
    observations, actions, data_variance = load_dataset(
        args.env,
        1,
        args.datapath,
        args.normal,
        args.num_data,
        args.stack,
        args.num_episodes,
    )
    ## Stage 1
    logging.info("Building models..")
    logging.info("Start stage 1...")

    env = create_atari_environment(args.env)
    action_dim = env.action_space.n

    n_batch = len(observations) // args.batch_size + 1
    total_idxs = list(range(len(observations)))

    logging.info("Training starts..")

    save_dir = "models_beta_vae_actor"

    if args.num_episodes is None:
        save_tag = "{}_s{}_data{}k_con{}_seed{}_ne{}".format(
            args.env,
            args.stack,
            int(args.num_data / 1000),
            1 - int(args.normal),
            args.seed,
            args.num_embeddings,
        )
    else:
        save_tag = "{}_s{}_epi{}_con{}_seed{}_ne{}".format(
            args.env,
            args.stack,
            int(args.num_episodes),
            1 - int(args.normal),
            args.seed,
            args.num_embeddings,
        )

    resize = kornia.geometry.Resize(64)
    save_dir = save_dir + "_coord_conv"

    save_dir = save_dir + "_graph_param"
    save_tag = save_tag + "_prob{}".format(args.prob)

    if args.add_path is not None:
        save_dir = save_dir + "_" + args.add_path
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    encoder = CoordConvEncoder(1, args.z_dim * 2, args.ch_div).to(device)

    actor = nn.Sequential(
        nn.Linear(args.z_dim * 2, args.z_dim),
        nn.ReLU(),
        nn.Linear(args.z_dim, action_dim),
    )
    actor.apply(weight_init)
    actor.to(device)

    for p in encoder.parameters():
        p.requires_grad = False

    if args.beta_vae_path is None:
        assert False
    beta_vae_dict = torch.load(args.beta_vae_path, map_location="cpu")
    encoder.load_state_dict(
        {k[8:]: v for k, v in beta_vae_dict.items() if "encoder" in k}
    )

    actor_optimizer = torch.optim.Adam(actor.parameters(), lr=args.lr)

    ## Multi-GPU
    if torch.cuda.device_count() > 1:
        encoder = nn.DataParallel(encoder)
        actor = nn.DataParallel(actor)

    criterion = nn.CrossEntropyLoss()
    scores = []
    logging.info("Training starts..")
    f_tr = open(os.path.join(save_dir, save_tag + "_cnn_train.csv"), "w")
    writer_tr = csv.writer(f_tr)
    writer_tr.writerow(["Epoch", "Loss", "Accuracy"])

    f_te = open(os.path.join(save_dir, save_tag + "_cnn_eval.csv"), "w")
    writer_te = csv.writer(f_te)
    writer_te.writerow(["Epoch", "Loss", "Accuracy", "Score"])

    for epoch in tqdm(range(args.n_epochs)):
        encoder.eval()
        actor.train()
        random.shuffle(total_idxs)
        actor_losses = []
        accuracies = []
        for j in range(n_batch):
            batch_idxs = total_idxs[j * args.batch_size : (j + 1) * args.batch_size]
            xx = torch.as_tensor(
                observations[batch_idxs], device=device, dtype=torch.float32
            )
            xx = xx / 255.0
            xx = resize(xx)

            batch_act = torch.as_tensor(actions[batch_idxs], device=device).long()

            actor_optimizer.zero_grad()

            with torch.no_grad():
                z = encoder(xx)
                z, _ = z.chunk(2, dim=-1)  # mu

            prob = torch.ones(z.size()) * (1 - args.prob)
            mask = torch.bernoulli(prob).to(device)
            z = torch.cat([z * mask, mask], dim=1)

            logits = actor(z)
            actor_loss = criterion(logits, batch_act)

            actor_loss.backward()

            actor_optimizer.step()

            accuracy = (batch_act == logits.argmax(1)).float().mean()

            actor_losses.append(actor_loss.mean().detach().cpu().item())
            accuracies.append(accuracy.mean().detach().cpu().item())

        logging.info(
            "(Train) Epoch {} | Actor Loss: {:.4f} | Accuracy: {:.2f}".format(
                epoch + 1, np.mean(actor_losses), np.mean(accuracies),
            )
        )
        writer_tr.writerow(
            [epoch + 1, np.mean(actor_losses), np.mean(accuracies),]
        )

        if (epoch + 1) % args.eval_interval == 0:
            actor.eval()
            encoder.eval()
            score = evaluate(
                env,
                nn.Identity(),
                actor.module if torch.cuda.device_count() > 1 else actor,
                encoder.module if torch.cuda.device_count() > 1 else encoder,
                "beta_vae",
                device,
                args,
            )
            logging.info("(Eval) Epoch {} | Score: {:.2f}".format(epoch + 1, score,))
            scores.append(score)
            actor.train()
            writer_te.writerow(
                [epoch + 1, np.mean(actor_losses), np.mean(accuracies), score]
            )

    f_tr.close()
    f_te.close()

    torch.save(
        encoder.module.state_dict()
        if torch.cuda.device_count() > 1
        else encoder.state_dict(),
        os.path.join(save_dir, save_tag + "_ep{}_encoder.pth".format(epoch + 1)),
    )
    torch.save(
        actor.module.state_dict()
        if torch.cuda.device_count() > 1
        else actor.state_dict(),
        os.path.join(save_dir, save_tag + "_ep{}_actor.pth".format(epoch + 1),),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Seed & Env
    parser.add_argument("--seed", default=1, type=int)
    parser.add_argument("--env", default="Pong", type=str)
    parser.add_argument("--datapath", default="/data", type=str)
    parser.add_argument("--num_data", default=50000, type=int)
    parser.add_argument("--stack", default=1, type=int)
    parser.add_argument("--normal", action="store_true", default=False)
    parser.add_argument("--normal_eval", action="store_true", default=False)

    # Save & Evaluation
    parser.add_argument("--save_interval", default=20, type=int)
    parser.add_argument("--eval_interval", default=20, type=int)
    parser.add_argument("--num_episodes", default=None, type=int)
    parser.add_argument("--num_eval_episodes", default=20, type=int)
    parser.add_argument("--n_epochs", default=1000, type=int)
    parser.add_argument("--add_path", default=None, type=str)

    # Encoder & Hyperparams
    parser.add_argument("--embedding_dim", default=64, type=int)
    parser.add_argument("--num_embeddings", default=512, type=int)
    parser.add_argument("--num_hiddens", default=128, type=int)
    parser.add_argument("--num_residual_layers", default=2, type=int)
    parser.add_argument("--num_residual_hiddens", default=32, type=int)
    parser.add_argument("--batch_size", default=1024, type=int)
    parser.add_argument("--lr", default=3e-4, type=float)

    # Model load
    parser.add_argument("--beta_vae_path", default=None, type=str)
    # For MLP
    parser.add_argument("--z_dim", default=50, type=int)
    # For dropout
    parser.add_argument("--prob", default=0.5, type=float)

    parser.add_argument("--ch_div", default=1, type=int)

    args = parser.parse_args()
    if args.normal:
        assert args.normal_eval
    else:
        assert not args.normal_eval

    args.coord_conv = True
    train(args)
