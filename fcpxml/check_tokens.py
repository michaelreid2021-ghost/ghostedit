#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 99_token_dry_run.py
# Purpose: Upload file -> Count Tokens -> Delete File (Dry Run)

import argparse
import sys
import time
import os
import logging
from pathlib import Path
import google.generativeai as genai

# --- FORCE UTF-8 OUTPUT (Windows Fix) ---
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# Setup logging (Matching your style)
script_dir = Path(__file__).resolve().parent
log_file_path = script_dir / "99_token_dry_run.log"
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = logging.FileHandler(log_file_path, mode='a', encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    logger.addHandler(stream_handler)

def dry_run_count(file_path: Path):
    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        sys.exit(1)

    # Check for API Key
    if not os.environ.get("GOOGLE_API_KEY"):
        logger.warning("GOOGLE_API_KEY environment variable not found. Attempting to run, but auth may fail.")

    try:
        # 1. Upload
        logger.info(f"Uploading {file_path.name} to Gemini File API...")
        file_ref = genai.upload_file(path=file_path)
        logger.info(f"Upload complete. URI: {file_ref.uri}")

        # 2. Wait for processing (Server-side tokenization requires processing)
        while file_ref.state.name == "PROCESSING":
            logger.info("Server is processing file...")
            time.sleep(2)
            file_ref = genai.get_file(file_ref.name)

        if file_ref.state.name == "FAILED":
            raise RuntimeError(f"File processing failed on server: {file_ref.state.name}")

        # 3. Count Tokens
        # We use Flash for counting as the tokenizer is identical to Pro, but it initializes faster
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        logger.info("Requesting token count...")
        response = model.count_tokens([file_ref])
        
        # 4. Output
        logger.info("------------------------------------------------")
        logger.info(f"File: {file_path.name}")
        logger.info(f"Total Tokens: {response.total_tokens}")
        logger.info("------------------------------------------------")

        # 5. Cleanup
        logger.info("Cleaning up (Deleting remote file)...")
        genai.delete_file(file_ref.name)
        logger.info("Cleanup complete.")

    except Exception as e:
        logger.error(f"An error occurred: {e}")
        # Attempt cleanup even on failure if file_ref exists
        try:
            if 'file_ref' in locals():
                genai.delete_file(file_ref.name)
                logger.info("Emergency cleanup complete.")
        except:
            pass
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Uploads a file, checks token count, and immediately deletes it.")
    parser.add_argument("--input_filename", type=str, required=True, help="Path to the video/audio/text file to check.")
    
    args = parser.parse_args()
    input_path = Path(args.input_filename).resolve()

    dry_run_count(input_path)

if __name__ == "__main__":
    main()