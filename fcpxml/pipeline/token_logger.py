#!/usr/bin/env python3
# token_logger.py
import csv
from pathlib import Path
import datetime

def log_token_usage(project_name: str, script_name: str, model_name: str, prompt_token_count: int, candidates_token_count: int, total_token_count: int):
    """Appends a record of AI token usage to a CSV file.

    Args:
        project_name (str): The name of the project.
        script_name (str): The name of the script making the AI call.
        model_name (str): The name of the AI model used.
        prompt_token_count (int): The number of tokens in the prompt.
        candidates_token_count (int): The number of tokens in the response.
        total_token_count (int): The total number of tokens used.
    """
    # This assumes the script is run from within the pipeline directory
    log_file = Path("ai_token_usage.csv").resolve()
    file_exists = log_file.exists()

    with open(log_file, "a", newline="") as csvfile:
        fieldnames = ["timestamp", "project_name", "script_name", "model_name", "prompt_tokens", "response_tokens", "total_tokens"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()
        
        writer.writerow({
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "project_name": project_name,
            "script_name": script_name,
            "model_name": model_name,
            "prompt_tokens": prompt_token_count,
            "response_tokens": candidates_token_count,
            "total_tokens": total_token_count
        })
