"""
tools/model_tuning_engine.py
============================
Centralized model fine-tuning engine and autonomous tool for Sabrina.
Enables fine-tuning, adaptation, and evaluation of:
  - BERT / SecureBERT sequence classification (e.g. MITRE ATT&CK tactics, threat triage)
  - BERT / DistilBERT token classification (e.g. Cybersecurity PII & artifact NER)
  - Causal LM SFT / LoRA (e.g. Exploit methodology reasoning, Chain-of-Thought)

Supports both local execution (CPU/CUDA) and remote offload to the lab's dedicated
NVIDIA RTX 5060 Ti host (192.168.2.69) via SSH/SFTP.
"""

import os
import sys
import json
import time
import logging
import argparse
import random
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

# Ensure UTF-8 output on Windows consoles to prevent cp1252 charmap errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [model_tuning] %(message)s")
logger = logging.getLogger("model_tuning_engine")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Remote Lab GPU Configuration
REMOTE_HOST = os.environ.get("SABRINA_TRAIN_HOST", "192.168.2.69")
REMOTE_USER = os.environ.get("SABRINA_TRAIN_USER", "ego")
REMOTE_PASS = os.environ.get("SABRINA_TRAIN_PASS", "ego")

# Standard NER Labels for Cybersecurity Artifacts
NER_TARGET_LABELS = [
    "O",
    "B-IP", "I-IP",
    "B-KEY", "I-KEY",
    "B-PASSWORD", "I-PASSWORD",
    "B-EMAIL", "I-EMAIL",
    "B-USERNAME", "I-USERNAME",
    "B-NAME", "I-NAME",
    "B-ID", "I-ID",
    "B-ORG", "I-ORG",
    "B-DOC", "I-DOC",
]
NER_LABEL2ID = {label: i for i, label in enumerate(NER_TARGET_LABELS)}
NER_ID2LABEL = {i: label for i, label in enumerate(NER_TARGET_LABELS)}

# Predefined Topic Presets
TOPIC_PRESETS = {
    "mitre_tactics": {
        "description": "14-class MITRE ATT&CK tactic classifier for SIEM, Sysmon, and EVTX logs",
        "task_type": "sequence_classification",
        "default_model": "ehsanaghaei/SecureBERT",
        "remote_dataset": "/home/ego/bot/encoder_dataset_14.jsonl",
        "remote_mapping": "/home/ego/bot/encoder_label_mapping_14.json",
        "local_dataset": str(PROJECT_ROOT / "data" / "encoder_dataset_14.jsonl"),
        "default_output_dir": str(PROJECT_ROOT / "models" / "securebert_classifier_14"),
        "remote_output_dir": "/home/ego/bot/securebert_classifier_14",
        "default_epochs": 3,
        "default_batch_size": 16,
        "default_lr": 3e-5,
    },
    "threat_triage": {
        "description": "Binary sequence classification (benign vs malicious) for high-throughput log gating",
        "task_type": "sequence_classification",
        "default_model": "ehsanaghaei/SecureBERT",
        "remote_dataset": "/home/ego/bot/triage_dataset_balanced.jsonl",
        "remote_mapping": "/home/ego/bot/triage_label_mapping.json",
        "local_dataset": str(PROJECT_ROOT / "data" / "triage_dataset_balanced.jsonl"),
        "default_output_dir": str(PROJECT_ROOT / "models" / "securebert_triage_binary"),
        "remote_output_dir": "/home/ego/bot/securebert_triage_binary",
        "default_epochs": 3,
        "default_batch_size": 16,
        "default_lr": 3e-5,
        "id2label": {0: "benign", 1: "malicious"},
        "label2id": {"benign": 0, "malicious": 1},
    },
    "cybersecurity_ner": {
        "description": "Token classification (NER) to detect and redact sensitive IPs, keys, credentials, and artifacts",
        "task_type": "token_classification",
        "default_model": "distilbert-base-cased",
        "local_dataset": "synthetic_or_bigcode",
        "remote_dataset": "synthetic_or_bigcode",
        "default_output_dir": str(PROJECT_ROOT / "models" / "sabrina-pii-ner"),
        "remote_output_dir": "/home/ego/bot/sabrina-pii-ner",
        "default_epochs": 3,
        "default_batch_size": 16,
        "default_lr": 2e-5,
        "id2label": NER_ID2LABEL,
        "label2id": NER_LABEL2ID,
    },
    "exploit_methodology_sft": {
        "description": "Causal LM LoRA fine-tuning for exploit reasoning, payload crafting, and CTF chain-of-thought",
        "task_type": "causal_lm",
        "default_model": "unsloth/Qwen2.5-7b-bnb-4bit",
        "local_dataset": str(PROJECT_ROOT / "data" / "curated_cybersec_SFT.jsonl"),
        "remote_dataset": "/home/ego/exploit_pipeline_service/exploit_sft_2k_final.jsonl",
        "default_output_dir": str(PROJECT_ROOT / "models" / "qwen2.5_exploit_methodology_lora"),
        "remote_output_dir": "/home/ego/exploit_pipeline_service/qwen2.5_exploit_methodology_lora",
        "default_epochs": 2,
        "default_batch_size": 1,
        "default_lr": 2e-4,
    },
    "json_structured_qa_lora": {
        "description": "Causal LM LoRA fine-tuning for strict JSON tool-calling, schema adherence, and zero conversational wrapping",
        "task_type": "causal_lm",
        "default_model": "unsloth/Qwen2.5-7b-bnb-4bit",
        "local_dataset": str(PROJECT_ROOT / "data" / "json_structured_qa.jsonl"),
        "remote_dataset": "/home/ego/bot/json_structured_qa.jsonl",
        "default_output_dir": str(PROJECT_ROOT / "models" / "qwen2.5_json_qa_lora"),
        "remote_output_dir": "/home/ego/bot/qwen2.5_json_qa_lora",
        "default_epochs": 2,
        "default_batch_size": 2,
        "default_lr": 2e-4,
    },
    "cybersecurity_qa_logic_sft": {
        "description": "Causal LM LoRA fine-tuning for evidence-based vulnerability verification, error-page discrimination, and secure API auth flows",
        "task_type": "causal_lm",
        "default_model": "unsloth/Qwen2.5-7b-bnb-4bit",
        "local_dataset": str(PROJECT_ROOT / "data" / "cybersecurity_qa_logic.jsonl"),
        "remote_dataset": "/home/ego/bot/cybersecurity_qa_logic.jsonl",
        "default_output_dir": str(PROJECT_ROOT / "models" / "qwen2.5_cybersec_qa_logic_lora"),
        "remote_output_dir": "/home/ego/bot/cybersecurity_qa_logic_lora",
        "default_epochs": 2,
        "default_batch_size": 2,
        "default_lr": 2e-4,
    },
    "crewai_sft": {
        "description": "Causal LM LoRA fine-tuning on CrewAI documentation",
        "task_type": "causal_lm",
        "default_model": "unsloth/Qwen2.5-7B-Instruct-bnb-4bit",
        "local_dataset": str(PROJECT_ROOT / "data" / "crewai_sft_dataset.jsonl"),
        "remote_dataset": "/home/ego/bot/data/crewai_sft_dataset.jsonl",
        "default_output_dir": str(PROJECT_ROOT / "models" / "qwen_crewai_lora"),
        "remote_output_dir": "/home/ego/bot/models/qwen_crewai_lora",
        "default_epochs": 3,
        "default_batch_size": 2,
        "default_lr": 2e-4,
    },
    "crewai_eval": {
        "description": "Arena CrewAI Evaluator run",
        "task_type": "causal_lm",
        "default_model": "qwen2.5:7b",
        "local_dataset": "",
        "remote_dataset": "",
        "default_output_dir": str(PROJECT_ROOT / "data"),
        "remote_output_dir": "/home/ego/bot/data",
        "default_epochs": 1,
        "default_batch_size": 1,
        "default_lr": 0,
    }
}


def is_local_cuda_available() -> bool:
    """Check if local PyTorch is installed and CUDA GPU is available."""
    try:
        import torch
        return torch.cuda.is_available() and torch.cuda.device_count() > 0
    except Exception:
        return False


def generate_synthetic_ner_dataset(num_samples: int = 1000):
    """Generate synthetic cybersecurity token classification samples."""
    from datasets import Dataset
    docs = ["CONFIDENTIAL", "Architecture V3", "Project Titan", "Source Code", 
            "Private Key", "Internal Audit", "Q3 Financials", "Restricted File"]
    orgs = ["CorpAcme", "EGO Revolution", "Initech", "Massive Dynamic", "Hooli"]
    
    tokens_list = []
    ner_tags_list = []
    
    for _ in range(num_samples):
        doc = random.choice(docs)
        org = random.choice(orgs)
        template_idx = random.randint(0, 2)
        if template_idx == 0:
            words = f"The {doc} document belongs to {org} .".split()
            tags = ["O", "B-DOC"] + ["I-DOC"] * (len(doc.split()) - 1) + ["O", "O", "O", "B-ORG"] + ["I-ORG"] * (len(org.split()) - 1) + ["O"]
        elif template_idx == 1:
            words = f"Access to {org} internal {doc} is restricted .".split()
            tags = ["O", "O", "B-ORG"] + ["I-ORG"] * (len(org.split()) - 1) + ["O", "B-DOC"] + ["I-DOC"] * (len(doc.split()) - 1) + ["O", "O", "O"]
        else:
            words = f"{org} leaked {doc} on github .".split()
            tags = ["B-ORG"] + ["I-ORG"] * (len(org.split()) - 1) + ["O", "B-DOC"] + ["I-DOC"] * (len(doc.split()) - 1) + ["O", "O", "O"]
            
        if len(words) == len(tags):
            tokens_list.append(words)
            ner_tags_list.append([NER_LABEL2ID.get(t, 0) for t in tags])
            
    return Dataset.from_dict({"tokens": tokens_list, "ner_tags": ner_tags_list})


def align_labels_with_tokens(labels, word_ids):
    """Aligns token classification NER tags with subword tokens."""
    new_labels = []
    current_word = None
    for word_id in word_ids:
        if word_id is None:
            new_labels.append(-100)
        elif word_id != current_word:
            new_labels.append(labels[word_id])
            current_word = word_id
        else:
            label = labels[word_id]
            if label % 2 == 1:
                new_labels.append(label + 1)
            else:
                new_labels.append(label)
    return new_labels


def run_local_training(
    task_type: str,
    base_model: str,
    dataset_path: str,
    output_dir: str,
    epochs: int = 3,
    batch_size: int = 16,
    lr: float = 3e-5,
    id2label: Optional[Dict[Any, str]] = None,
    label2id: Optional[Dict[str, Any]] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """Executes fine-tuning locally using PyTorch & Hugging Face Transformers."""
    t0 = time.time()
    logger.info(f"Initiating local training: task={task_type}, model={base_model}, out={output_dir}")
    
    try:
        import torch
        from transformers import AutoTokenizer, TrainingArguments, Trainer
    except ImportError as e:
        return {"status": "ERROR", "error": f"Required packages missing locally: {e}"}

    os.makedirs(output_dir, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"Local training executing on device: {device}")

    # 1. Sequence Classification Pipeline
    if task_type == "sequence_classification":
        from transformers import AutoModelForSequenceClassification
        from datasets import Dataset

        texts, labels = [], []
        if dataset_path and os.path.exists(dataset_path):
            with open(dataset_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            item = json.loads(line)
                            texts.append(item["text"])
                            labels.append(int(item["label"]))
                        except Exception:
                            continue
        else:
            return {"status": "ERROR", "error": f"Dataset path {dataset_path} not found locally. Cannot tune model without actual training data."}

        ds = Dataset.from_dict({"text": texts, "label": labels}).train_test_split(test_size=0.1, seed=42)
        tokenizer = AutoTokenizer.from_pretrained(base_model)
        tokenized_ds = ds.map(lambda b: tokenizer(b["text"], padding="max_length", truncation=True, max_length=256), batched=True)

        num_labels = len(id2label) if id2label else len(set(labels))
        model = AutoModelForSequenceClassification.from_pretrained(
            base_model,
            num_labels=num_labels,
            id2label=id2label,
            label2id=label2id,
            ignore_mismatched_sizes=True
        )

        training_args = TrainingArguments(
            output_dir=output_dir,
            eval_strategy="epoch",
            save_strategy="epoch",
            learning_rate=lr,
            per_device_train_batch_size=batch_size,
            per_device_eval_batch_size=batch_size,
            num_train_epochs=epochs,
            weight_decay=0.01,
            fp16=(device == "cuda"),
            report_to="none"
        )

        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=tokenized_ds["train"],
            eval_dataset=tokenized_ds["test"],
            processing_class=tokenizer,
        )
        
        if not dry_run:
            trainer.train()
            trainer.save_model(output_dir)
            tokenizer.save_pretrained(output_dir)
            if id2label:
                with open(os.path.join(output_dir, "labels.json"), "w", encoding="utf-8") as f:
                    json.dump({"id2label": id2label, "label2id": label2id}, f, indent=2)

        return {
            "status": "SUCCESS (DRY_RUN)" if dry_run else "SUCCESS",
            "backend": f"local_{device}",
            "task_type": task_type,
            "base_model": base_model,
            "output_dir": output_dir,
            "elapsed_seconds": round(time.time() - t0, 2)
        }

    # 2. Token Classification (NER) Pipeline
    elif task_type == "token_classification":
        from transformers import AutoModelForTokenClassification, DataCollatorForTokenClassification
        from datasets import load_dataset
        
        tokenizer = AutoTokenizer.from_pretrained(base_model)
        
        if not dataset_path:
            return {"status": "ERROR", "error": "Dataset path or HF ID missing. Token classification requires real data."}
            
        try:
            if os.path.exists(dataset_path):
                real_ds = load_dataset('json', data_files=dataset_path, split='train')
            else:
                logger.info(f"Dataset not found locally, attempting to fetch {dataset_path} from Hugging Face...")
                real_ds = load_dataset(dataset_path, split='train')
        except Exception as e:
            return {"status": "ERROR", "error": f"Failed to load dataset {dataset_path}: {e}"}
        
        def tokenize_ner(examples):
            tokenized_inputs = tokenizer(examples["tokens"], truncation=True, is_split_into_words=True, max_length=256, padding="max_length")
            all_labels = []
            for i, labs in enumerate(examples["ner_tags"]):
                word_ids = tokenized_inputs.word_ids(batch_index=i)
                all_labels.append(align_labels_with_tokens(labs, word_ids))
            tokenized_inputs["labels"] = all_labels
            return tokenized_inputs

        tokenized_ds = real_ds.map(tokenize_ner, batched=True).train_test_split(test_size=0.1, seed=42)
        model = AutoModelForTokenClassification.from_pretrained(
            base_model,
            num_labels=len(NER_TARGET_LABELS),
            id2label=NER_ID2LABEL,
            label2id=NER_LABEL2ID
        )

        training_args = TrainingArguments(
            output_dir=output_dir,
            eval_strategy="epoch",
            save_strategy="epoch",
            learning_rate=lr,
            per_device_train_batch_size=batch_size,
            per_device_eval_batch_size=batch_size,
            num_train_epochs=epochs,
            weight_decay=0.01,
            fp16=(device == "cuda"),
            report_to="none"
        )
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=tokenized_ds["train"],
            eval_dataset=tokenized_ds["test"],
            processing_class=tokenizer,
            data_collator=DataCollatorForTokenClassification(tokenizer=tokenizer),
        )
        
        if not dry_run:
            trainer.train()
            trainer.save_model(output_dir)
            tokenizer.save_pretrained(output_dir)

        return {
            "status": "SUCCESS (DRY_RUN)" if dry_run else "SUCCESS",
            "backend": f"local_{device}",
            "task_type": task_type,
            "base_model": base_model,
            "output_dir": output_dir,
            "elapsed_seconds": round(time.time() - t0, 2)
        }

    # 3. Causal LM (LoRA SFT) Pipeline
    elif task_type == "causal_lm":
        try:
            from transformers import AutoModelForCausalLM
            from peft import LoraConfig, get_peft_model, TaskType
            from trl import SFTTrainer, SFTConfig
            from datasets import load_dataset
        except ImportError as e:
            return {"status": "ERROR", "error": f"Missing PEFT/TRL for causal_lm: {e}"}

        if not dataset_path:
            return {"status": "ERROR", "error": "Dataset path or HF ID missing. Causal LM fine-tuning requires real data."}

        tokenizer = AutoTokenizer.from_pretrained(base_model)
        tokenizer.pad_token = tokenizer.eos_token
        
        model_kwargs = {"device_map": "auto"} if device == "cuda" else {}
        if device == "cuda":
            import torch
            model_kwargs["torch_dtype"] = torch.float16

        model = AutoModelForCausalLM.from_pretrained(base_model, **model_kwargs)
        
        lora_config = LoraConfig(
            r=8,
            lora_alpha=16,
            target_modules=["q_proj", "v_proj"],
            lora_dropout=0.05,
            bias="none",
            task_type=TaskType.CAUSAL_LM
        )
        
        model = get_peft_model(model, lora_config)
        
        try:
            if os.path.exists(dataset_path):
                dataset = load_dataset('json', data_files=dataset_path, split='train')
            else:
                logger.info(f"Dataset not found locally, attempting to fetch {dataset_path} from Hugging Face...")
                dataset = load_dataset(dataset_path, split='train')
        except Exception as e:
            return {"status": "ERROR", "error": f"Failed to load dataset {dataset_path}: {e}"}

        sft_config = SFTConfig(
            output_dir=output_dir,
            dataset_text_field="text",
            max_seq_length=512,
            per_device_train_batch_size=batch_size,
            gradient_accumulation_steps=4,
            learning_rate=lr,
            num_train_epochs=epochs,
            fp16=(device == "cuda"),
            report_to="none"
        )

        trainer = SFTTrainer(
            model=model,
            args=sft_config,
            train_dataset=dataset,
            processing_class=tokenizer,
        )
        
        if not dry_run:
            trainer.train()
            trainer.save_model(output_dir)
            tokenizer.save_pretrained(output_dir)
            
        return {
            "status": "SUCCESS (DRY_RUN)" if dry_run else "SUCCESS",
            "backend": f"local_{device}",
            "task_type": task_type,
            "base_model": base_model,
            "output_dir": output_dir,
            "elapsed_seconds": round(time.time() - t0, 2)
        }

    else:
        return {"status": "ERROR", "error": f"Unsupported local task_type: {task_type}."}


def run_remote_gpu_training(topic, preset, epochs, batch_size, lr, timeout=None, dry_run=False):
    if dry_run:
        return _run_remote_gpu_training_owned(topic, preset, epochs, batch_size, lr, timeout, dry_run)
    from tools.control_client import resource_lease
    started = False
    try:
        with resource_lease('compute:lab'):
            started = True
            result = _run_remote_gpu_training_owned(topic, preset, epochs, batch_size, lr, timeout, dry_run)
            if result.get('status') == 'ERROR':
                raise RuntimeError(result.get('error', 'Remote training outcome uncertain'))
            return result
    except Exception as exc:
        return {'status': 'DEFERRED' if isinstance(exc, TimeoutError) and not started else 'ERROR',
                'error': str(exc), 'host': REMOTE_HOST}


def _run_remote_gpu_training_owned(
    topic: str,
    preset: Dict[str, Any],
    epochs: int,
    batch_size: int,
    lr: float,
    timeout: Optional[int] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """Offloads training job to the lab's dedicated RTX 5060 Ti host via Paramiko SSH."""
    t0 = time.time()
    logger.info(f"Connecting to remote GPU host {REMOTE_HOST} for topic '{topic}'...")

    if dry_run:
        time.sleep(1.0)
        return {
            "status": "SUCCESS (DRY_RUN)",
            "backend": f"remote_gpu ({REMOTE_HOST})",
            "topic": topic,
            "base_model": preset.get("default_model"),
            "epochs": epochs,
            "elapsed_seconds": round(time.time() - t0, 2),
            "remote_output_dir": preset.get("remote_output_dir"),
            "stdout_tail": "Dry-run simulated remote GPU dispatch complete."
        }

    try:
        import paramiko
    except ImportError:
        return {"status": "ERROR", "error": "Paramiko library not installed for remote SSH offload."}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(REMOTE_HOST, username=REMOTE_USER, password=REMOTE_PASS, timeout=15)
        logger.info("SSH connection established with lab GPU host.")

        from tools.control_client import configured
        if configured():
            # A resource lease excludes managed inference, but unrelated processes may exist.
            # Wait for residency to expire instead of stopping arbitrary Ollama workloads.
            _, gpu_out, _ = client.exec_command('nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits', timeout=10)
            used = [float(v) for v in gpu_out.read().decode().splitlines() if v.strip()]
            if not used or max(used) > 1024:
                return {'status': 'DEFERRED', 'error': 'Waiting for GPU memory to become free'}
        else:
          try:
            logger.info("Evicting parked Ollama models to ensure 100% GPU VRAM availability...")
            client.exec_command("for m in $(ollama ps 2>/dev/null | awk 'NR>1 {print $1}'); do ollama stop $m; done", timeout=10)
          except Exception as e:
            logger.warning(f"Note on VRAM pre-clearance: {e}")

        # Determine remote command based on topic
        if topic == "mitre_tactics":
            cmd = f"python3 /home/ego/bot/train_securebert.py"
        elif topic == "threat_triage":
            cmd = f"python3 /home/ego/bot/train_triage_model.py"
        elif topic == "cybersecurity_ner":
            cmd = f"python3 /home/ego/bot/train_diverse_securebert.py"
        elif topic == "exploit_methodology_sft":
            cmd = "cd /home/ego/exploit_pipeline_service && python3 train_lora_exploit_sft.py"
        elif topic == "crewai_sft":
            cmd = "cd /home/ego/bot && python3 train_lora_crewai.py"
        elif topic == "crewai_eval":
            cmd = "cd /home/ego/bot && python3 arena_crewai_evaluator.py"
        else:
            cmd = f"python3 /home/ego/bot/train_triage_model.py"

        timeout_val = timeout or (7200 if topic == "exploit_methodology_sft" else 1800)
        logger.info(f"Executing remote command (timeout={timeout_val}s): {cmd}")
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout_val)
        
        # Stream or capture log
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")

        lines = out.strip().split("\n")
        tail = "\n".join(lines[-25:]) if len(lines) > 25 else out

        return {
            "status": "SUCCESS" if ("Training Complete" in out or "Saving final" in out or "complete" in out.lower()) else "FINISHED",
            "backend": f"remote_gpu ({REMOTE_HOST} - NVIDIA RTX 5060 Ti)",
            "topic": topic,
            "remote_output_dir": preset.get("remote_output_dir"),
            "elapsed_seconds": round(time.time() - t0, 2),
            "stdout_tail": tail,
            "stderr": err[:500] if err.strip() else None
        }

    except Exception as e:
        err_msg = f"{type(e).__name__}: {str(e)}" if str(e) else type(e).__name__
        logger.error(f"Remote GPU training failed: {err_msg}")
        return {"status": "ERROR", "error": err_msg, "host": REMOTE_HOST}
    finally:
        client.close()


def tune_model(action_dict: Dict[str, Any]) -> str:
    """
    Main tool handler for Sabrina.
    
    Accepts:
        action_dict: {
            "tool": "tune_model",
            "topic": "mitre_tactics | threat_triage | cybersecurity_ner | exploit_methodology_sft | custom",
            "task_type": "sequence_classification | token_classification | causal_lm",
            "base_model": "<optional_model_id>",
            "dataset": "<optional_dataset_path_or_hf_id>",
            "epochs": 3,
            "batch_size": 16,
            "target_host": "auto | remote_gpu | local",
            "dry_run": false
        }
    """
    topic = action_dict.get("topic", "threat_triage").lower().strip()
    target_host = action_dict.get("target_host", "auto").lower().strip()
    dry_run = bool(action_dict.get("dry_run", False))
    epochs = int(action_dict.get("epochs", 3))
    batch_size = int(action_dict.get("batch_size", 16))
    lr = float(action_dict.get("learning_rate", 3e-5))
    timeout = action_dict.get("timeout")
    if timeout is not None:
        timeout = int(timeout)

    preset = TOPIC_PRESETS.get(topic, {})
    task_type = action_dict.get("task_type") or preset.get("task_type", "sequence_classification")
    base_model = action_dict.get("base_model") or preset.get("default_model", "ehsanaghaei/SecureBERT")
    dataset_path = action_dict.get("dataset") or preset.get("local_dataset", "")
    output_dir = action_dict.get("output_dir") or preset.get("default_output_dir", str(PROJECT_ROOT / "models" / f"tuned_{topic}"))

    # Resolve target_host if auto
    if target_host == "auto":
        # If task is causal_lm or local CUDA is absent, prefer remote GPU
        if task_type == "causal_lm" or not is_local_cuda_available():
            target_host = "remote_gpu"
        else:
            target_host = "local"

    logger.info(f"Dispatching tune_model: topic='{topic}', task='{task_type}', target='{target_host}'")

    if target_host == "remote_gpu":
        result = run_remote_gpu_training(
            topic=topic,
            preset=preset,
            epochs=epochs,
            batch_size=batch_size,
            lr=lr,
            timeout=timeout,
            dry_run=dry_run
        )
    else:
        result = run_local_training(
            task_type=task_type,
            base_model=base_model,
            dataset_path=dataset_path,
            output_dir=output_dir,
            epochs=epochs,
            batch_size=batch_size,
            lr=lr,
            id2label=preset.get("id2label"),
            label2id=preset.get("label2id"),
            dry_run=dry_run
        )

    # Format result for Sabrina's decision history
    output_str = f"""[Model Tuning Result]
- Status: {result.get('status')}
- Topic: {topic}
- Task Type: {task_type}
- Base Model: {base_model}
- Execution Target: {result.get('backend', target_host)}
- Elapsed Time: {result.get('elapsed_seconds', 0)}s
- Output Location: {result.get('output_dir') or result.get('remote_output_dir')}
"""
    if "metrics" in result:
        output_str += f"- Metrics: {json.dumps(result['metrics'])}\n"
    if "stdout_tail" in result:
        output_str += f"- Output Summary:\n{result['stdout_tail']}\n"
    if "error" in result:
        output_str += f"- Error Details: {result['error']}\n"

    return output_str


def main():
    parser = argparse.ArgumentParser(description="Sabrina Centralized Model Tuning Engine")
    parser.add_argument("--topic", type=str, default="threat_triage", choices=list(TOPIC_PRESETS.keys()) + ["custom"])
    parser.add_argument("--task-type", type=str, default=None, choices=["sequence_classification", "token_classification", "causal_lm"])
    parser.add_argument("--model", type=str, default=None, help="Base model to fine-tune")
    parser.add_argument("--dataset", type=str, default=None, help="Path to training dataset")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory for weights")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--target-host", type=str, default="auto", choices=["auto", "local", "remote_gpu"])
    parser.add_argument("--dry-run", action="store_true", help="Simulate pipeline without training compute")
    args = parser.parse_args()

    action = {
        "topic": args.topic,
        "task_type": args.task_type,
        "base_model": args.model,
        "dataset": args.dataset,
        "output_dir": args.output_dir,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "target_host": args.target_host,
        "dry_run": args.dry_run
    }
    print(tune_model(action))


if __name__ == "__main__":
    main()
