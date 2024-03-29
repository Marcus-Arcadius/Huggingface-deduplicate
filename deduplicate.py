import gzip
import hashlib
import json
import multiprocessing
import os
import shutil
import time
import re
from pathlib import Path

import numpy as np
from datasets import load_dataset

from arguments import PreprocessingArguments
from minhash_deduplication import deduplicate_dataset
from transformers import HfArgumentParser

PATTERN = re.compile(r"\s+")

def get_hash(example):
    """Get hash of content field."""
    return {"hash": hashlib.md5(re.sub(PATTERN, "", example["text"]).encode("utf-8")).hexdigest()}


def alpha_stats(example):
    """."""
    alpha_frac = np.mean([c.isalnum() for c in example["text"]])
    return {"alpha_frac": alpha_frac}


def line_stats(example):
    """Calculates mean and max line length of file."""
    line_lengths = [len(line) for line in example["text"].splitlines()]
    return {"line_len": line_lengths}


def check_uniques(example, uniques):
    """Check if current hash is still in set of unique hashes and remove if true."""
    if example["hash"] in uniques:
        uniques.remove(example["hash"])
        return True
    else:
        return False


def preprocess(example):
    """Chain all preprocessing steps into one function to not fill cache."""
    results = dict()
    results.update(get_hash(example))
    results.update(alpha_stats(example))
    results.update(line_stats(example))
    return results


def filter(example, uniques, args):
    """Filter dataset with heuristics. Config, test and has_no_keywords files are removed with a given probability."""
    if not check_uniques(example, uniques):
        return False
    elif example["alpha_frac"] < args.alpha_frac:
        return False
    elif example["line_len"] == []:
        return False
    else:
        return True


def compress_file(file_path):
    """Compress a file with g-zip."""
    with open(file_path, "rb") as f_in:
        with gzip.open(str(file_path) + ".gz", "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
    os.unlink(file_path)


# Settings
parser = HfArgumentParser(PreprocessingArguments)
args = parser.parse_args()

# Load dataset
t_start = time.time()
ds = load_dataset(args.dataset_name, args.dataset_subset, split="train")
print(f"Time to load dataset: {time.time()-t_start:.2f}")

# Run preprocessing
t_start = time.time()
num_workers = multiprocessing.cpu_count()
ds = ds.map(preprocess, num_proc = num_workers)
print(f"Time to preprocess dataset: {time.time()-t_start:.2f}")

# Deduplicate hashes
uniques = set(ds["hash"])
frac = len(uniques) / len(ds)
print(f"Fraction of duplicates: {1-frac:.2%}")

# Deduplicate data and apply heuristics
t_start = time.time()
ds_filter = ds.filter(filter, fn_kwargs={"uniques": uniques, "args": args})
print(f"Time to filter dataset: {time.time()-t_start:.2f}")
print(f"Size of filtered dataset: {len(ds_filter)}")

# Deduplicate with minhash and jaccard similarity
t_start = time.time()
ds_filter, duplicate_clusters = deduplicate_dataset(ds_filter, args.jaccard_threshold)
print(f"Time to deduplicate dataset: {time.time()-t_start:.2f}")
print(f"Size of deduplicate dataset: {len(ds_filter)}")

# Save data in batches of samples_per_file
output_dir = Path(args.output_dir)
output_dir.mkdir(exist_ok=True)

# save duplicate_clusters in the output_dir as artifacts
# not sure it is the right place the save it
with open(output_dir / "duplicate_clusters.json", "w") as f:
    json.dump(duplicate_clusters, f)

data_dir = output_dir / "data"
data_dir.mkdir(exist_ok=True)

t_start = time.time()
for file_number, index in enumerate(range(0, len(ds_filter), args.samples_per_file)):
    file_path = str(data_dir / f"file-{file_number+1:012}.json")
    end_index = min(len(ds_filter), index + args.samples_per_file)
    ds_filter.select(list(range(index, end_index))).to_json(file_path)
    compress_file(file_path)
print(f"Time to save dataset: {time.time()-t_start:.2f}")