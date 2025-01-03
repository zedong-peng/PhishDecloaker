"""Fine-tune a BiT model on some downstream dataset."""
import os
import time
from os.path import join as pjoin  # pylint: disable=g-importing-member

import numpy as np

# from torch.utils.tensorboard import SummaryWriter
import phishintention.src.crp_classifier_utils.bit_pytorch.fewshot as fs
import phishintention.src.crp_classifier_utils.bit_pytorch.models as models
import torch
import torchvision as tv
from phishintention.src.crp_classifier_utils import bit_common, bit_hyperrule
from phishintention.src.crp_classifier_utils.bit_pytorch.dataloader import (
    HybridLoader,
    LayoutLoader,
    ScreenshotLoader,
)

# from torchsummary import summary


os.environ["CUDA_VISIBLE_DEVICES"] = "1,0"


def recycle(iterable):
    """Variant of itertools.cycle that does not save iterates."""
    while True:
        for i in iterable:
            yield i


def mktrainval(args, logger):
    """Returns train and validation datasets."""
    #     train_set = ScreenshotLoader(img_folder='../datasets/train_merge_imgs',
    #                           annot_path='../datasets/train_al_merge_coords2.txt')
    # #
    #     val_set = ScreenshotLoader(img_folder='../datasets/val_merge_imgs',
    #                          annot_path='../datasets/val_merge_coords.txt')
    # #
    #     train_set = LayoutLoader(img_folder='../datasets/train_merge_imgs',
    #                           annot_path='../datasets/train_al_merge_coords2.txt')
    # #
    #     val_set = LayoutLoader(img_folder='../datasets/val_merge_imgs',
    #                          annot_path='../datasets/val_merge_coords.txt')

    train_set = HybridLoader(
        img_folder="../datasets/train_imgs", annot_path="../datasets/train_coords.txt"
    )

    val_set = HybridLoader(
        img_folder="../datasets/val_merge_imgs",
        annot_path="../datasets/val_merge_coords.txt",
    )

    if args.examples_per_class is not None:
        logger.info(f"Looking for {args.examples_per_class} images per class...")
        indices = fs.find_fewshot_indices(train_set, args.examples_per_class)
        train_set = torch.utils.data.Subset(train_set, indices=indices)

    logger.info(f"Using a training set with {len(train_set)} images.")
    logger.info(f"Using a validation set with {len(val_set)} images.")
    logger.info(f"Num of classes: {len(val_set.classes)}")

    valid_loader = torch.utils.data.DataLoader(
        val_set,
        batch_size=args.batch,
        shuffle=False,
        num_workers=args.workers,
        pin_memory=True,
        drop_last=False,
    )

    train_loader = torch.utils.data.DataLoader(
        train_set,
        batch_size=args.batch,
        shuffle=True,
        num_workers=args.workers,
        pin_memory=True,
        drop_last=False,
    )

    return train_set, val_set, train_loader, valid_loader


def run_eval(model, data_loader, device, logger, step):
    # Switch to evaluate mode
    model.eval()
    logger.info("Running validation...")
    logger.flush()

    correct = 0
    total = 0
    for b, (x, y, _) in enumerate(data_loader):
        with torch.no_grad():
            x = x.to(device, dtype=torch.float)
            y = y.to(device, dtype=torch.long)

            # Compute output, measure accuracy
            logits = model(x)
            preds = torch.argmax(logits, dim=1)
            correct += preds.eq(y).sum().item()
            total += len(logits)
            print(float(correct / total))

    model.train()
    logger.info(f"top1 {float(correct/total):.2%}, ")
    logger.flush()
    return float(correct / total)


def main(args):
    logger = bit_common.setup_logger(args)

    # Lets cuDNN benchmark conv implementations and choose the fastest.
    # Only good if sizes stay the same within the main loop!
    torch.backends.cudnn.benchmark = True

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Going to train on {device}")

    train_set, valid_set, train_loader, valid_loader = mktrainval(args, logger)
    model = models.KNOWN_MODELS[args.model](head_size=len(valid_set.classes))
    model = torch.nn.DataParallel(model)
    model = model.to(device)

    # Note: no weight-decay!
    step = 0
    optim = torch.optim.SGD(model.parameters(), lr=args.base_lr, momentum=0.9)

    # If pretrained weights are specified
    if args.weights_path:
        logger.info("Loading weights from {}".format(args.weights_path))
        checkpoint = torch.load(args.weights_path, map_location="cpu")
        # New task might have different classes; remove the pretrained classifier weights
        del checkpoint["model"]["module.head.conv.weight"]
        del checkpoint["model"]["module.head.conv.bias"]
        model.load_state_dict(checkpoint["model"], strict=False)

    # Resume fine-tuning if we find a saved model.
    savename = pjoin(
        args.logdir, args.name, "{}_{}.pth.tar".format(args.model, str(args.base_lr))
    )
    try:
        checkpoint = torch.load(savename, map_location="cpu")
        logger.info(f"Found saved model to resume from at '{savename}'")
        step = checkpoint["step"]
        model.load_state_dict(checkpoint["model"])
        optim.load_state_dict(checkpoint["optim"])
        logger.info(f"Resumed at step {step}")
    except FileNotFoundError:
        logger.info("Training from scratch")

    # Print out the model summary
    logger.info("Moving model onto all GPUs")
    model = model.to(device)
    logger.info(model)

    # Start training
    model.train()
    cri = torch.nn.CrossEntropyLoss().to(device)

    logger.info("Starting training!")

    for x, y, _ in recycle(train_loader):
        print("Batch input shape:", x.shape)
        print("Batch target shape:", y.shape)

        # Schedule sending to GPU(s)
        x = x.to(device, dtype=torch.float)
        y = y.to(device, dtype=torch.long)
        x.requires_grad = True

        # Update learning-rate, including stop training if over.
        lr = bit_hyperrule.get_lr(
            step=step, dataset_size=len(train_set), base_lr=args.base_lr
        )
        if lr is None:
            break
        for param_group in optim.param_groups:
            param_group["lr"] = lr

        # Compute output
        logits = model(x)
        c = cri(logits, y)
        c_num = float(c.data.cpu().numpy())  # Also ensures a sync point.

        # BP
        optim.zero_grad()
        c.backward()
        optim.step()
        step += 1

        # Write
        logger.info(
            f"[step {step}]: loss={c_num:.5f} (lr={lr})"
        )  # pylint: disable=logging-format-interpolation
        logger.flush()

        # Get train_acc every 1 epoch
        if step % (len(train_set) // args.batch) == 0:
            correct_rate = run_eval(model, valid_loader, device, logger, step)

            # Save model at best validation accuracy
            logger.info(
                f"Save model at step {step} or epoch {step // (len(train_set)//args.batch)}"
            )
            logger.info(f"Validation accuracy {correct_rate}")
            torch.save(
                {
                    "step": step,
                    "model": model.state_dict(),
                    "optim": optim.state_dict(),
                },
                savename,
            )

    # Final evaluation at the end of training
    correct_rate = run_eval(model, valid_loader, device, logger, step)
    torch.save(
        {
            "step": step,
            "model": model.state_dict(),
            "optim": optim.state_dict(),
        },
        savename,
    )


if __name__ == "__main__":
    parser = bit_common.argparser(models.KNOWN_MODELS.keys())
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Number of background threads used to load data.",
    )
    parser.add_argument("--no-save", dest="save", action="store_false")
    main(parser.parse_args())
