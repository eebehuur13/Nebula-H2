"""Measure how fast this machine turns text into search entries, at a given number of threads.

Run inside the app image, with the CPU limit you want to test, for example on your server:

    sudo docker run --rm --cpus 3 -e NEBULA_EMBED_PROVIDER=local -e NEBULA_LOCAL_MODEL_THREADS=3 \\
        <image> python -m nebula.bench

It embeds passages of about 1,800 characters (the size Nebula stores) in batches of four, like
the worker does, and needs no database.
"""

from __future__ import annotations

import argparse
import time

PASSAGE = (
    "The pilot runs in twenty anganwadi centres across four districts. Parents get one WhatsApp nudge "
    "a day with an activity to do with their child, a short quiz every Saturday, and a home visit from "
    "the centre worker once a month. आंगनबाड़ी केंद्र में बच्चों की उपस्थिति हर महीने दर्ज की जाती है। "
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--passages", type=int, default=24, help="how many passages to embed (default 24)")
    args = parser.parse_args()

    from nebula import models
    from nebula.config import settings

    s = settings()
    text = (PASSAGE * 8)[:1800]
    passages = [f"{i}. {text}" for i in range(args.passages)]
    t0 = time.perf_counter()
    models._local_embedder().embed(["warm up"], batch_size=1).__next__()
    load = time.perf_counter() - t0
    t1 = time.perf_counter()
    for i in range(0, len(passages), models.EMBED_BATCH):
        list(models._local_embedder().embed(passages[i:i + models.EMBED_BATCH], batch_size=models.EMBED_BATCH))
    took = time.perf_counter() - t1
    print(f"threads={s.local_model_threads or 'default'}  passages={len(passages)}  "
          f"seconds={took:.1f}  per_passage={took / len(passages):.2f}s  "
          f"passages_per_minute={len(passages) / took * 60:.0f}  (model load {load:.1f}s)")


if __name__ == "__main__":
    main()
