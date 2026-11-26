"""
Sabrina Hugging Face Tools
==========================
Tools for Sabrina to autonomously traverse, search, and catalog data from 
the Hugging Face Hub (datasets, models, papers) into her centralized RAG database.
"""

import logging
import requests
import subprocess
import sys
from typing import Dict, Any


logger = logging.getLogger("sabrina.huggingface_tools")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

HF_API_BASE = "https://huggingface.co/api"

def search_huggingface_hub(query: str, repo_type: str = "dataset") -> str:
    """Anonymous bounded discovery; model-card claims are not capability evidence."""
    import json
    kinds={"model":"models","models":"models","dataset":"datasets","datasets":"datasets","paper":"papers","papers":"papers"}
    if not isinstance(query,str) or not 1 <= len(query.strip()) <= 200:
        return "Error: search query must contain 1..200 characters."
    if not isinstance(repo_type,str) or repo_type.lower() not in kinds:
        return "Error: repo_type must be model, dataset, or paper."
    api_path=kinds[repo_type.lower()]
    try:
        with requests.Session() as session:
            session.trust_env=False  # Public metadata: no saved netrc authentication.
            with session.get(f"{HF_API_BASE}/{api_path}",params={"search":query.strip(),"limit":5,"full":"true"},
                             timeout=(10,20),stream=True) as response:
                response.raise_for_status();payload=bytearray()
                for chunk in response.iter_content(16384):
                    payload.extend(chunk)
                    if len(payload)>2*1024**2:raise ValueError('Hub search response exceeded 2 MiB')
        data=json.loads(payload)
        if not isinstance(data,list):raise ValueError('Unexpected Hub search response')
        rows=[]
        for item in data[:5]:
            card=item.get('cardData') or {}
            rows.append({'repo_id':item.get('id'),'revision':item.get('sha'),'gated':item.get('gated'),
                         'downloads':item.get('downloads'),'declared_base':card.get('base_model'),
                         'declared_license':card.get('license'),'description':str(item.get('description') or '')[:250]})
        return json.dumps({'query':query.strip(),'repo_type':api_path,'results':rows,
            'scope':'Public discovery metadata only. Pin full revisions, inspect actual weight artifacts and compatibility, then evaluate through MRI. Downloads and author claims do not prove capability.',
            'weights_downloaded':False,'inference_performed':False,'authentication_used':False},ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to search Hugging Face Hub: {e}")
        return f"Error searching Hugging Face: {str(e)}"


def catalog_huggingface_repo(repo_id: str, repo_type: str = "dataset") -> str:
    """
    Download a Hugging Face repository's README, distill it, and inject it 
    into Sabrina's RAG database for long-term memory.
    
    Args:
        repo_id: Exact HF repository ID (e.g., 'CMCC-ROCK/Pentest')
        repo_type: 'dataset' or 'model'
        
    Returns:
        Status summary of the knowledge insertion.
    """
    from tools.rag_tools import get_rag_db, get_synthesizer
    repo_type = repo_type.lower()
    if repo_type in ["datasets", "dataset"]:
        raw_url = f"https://huggingface.co/datasets/{repo_id}/raw/main/README.md"
        source_url = f"https://huggingface.co/datasets/{repo_id}"
    else:
        raw_url = f"https://huggingface.co/{repo_id}/raw/main/README.md"
        source_url = f"https://huggingface.co/{repo_id}"
        
    try:
        # Fetch the raw README markdown
        logger.info(f"Fetching raw README from {raw_url}")
        response = requests.get(raw_url, timeout=10)
        
        if response.status_code == 404:
            return f"Could not find a README.md for '{repo_id}'. It may not exist or the repository is empty."
        response.raise_for_status()
        
        raw_markdown = response.text
        
        title = f"Hugging Face {repo_type.capitalize()}: {repo_id}"
        
        synth = get_synthesizer()
        db = get_rag_db()
        
        # Insert Document metadata
        doc_id, _ = db.insert_document(
            url=source_url,
            title=title,
            raw_content=raw_markdown,
            source_type="huggingface"
        )
        
        logger.info("Synthesizing repository metadata via Lab LLM...")
        chunks = synth.synthesize_chunks(title, raw_markdown[:15000])
        
        if not chunks:
            return f"Failed to synthesize content for '{repo_id}'."
            
        saved_chunk_ids = []
        for idx, chunk in enumerate(chunks):
            topic = chunk["topic"]
            content = chunk["content"]
            
            logger.info(f"Generating vector embedding for chunk {idx}...")
            embedding = synth.get_embedding(f"{topic}\n{content}")
            
            chunk_id = db.insert_chunk(
                document_id=doc_id,
                chunk_index=idx,
                topic=topic,
                content=content,
                embedding=embedding
            )
            saved_chunk_ids.append(chunk_id)
        
        return f"Successfully cataloged {repo_type} '{repo_id}' into RAG memory ({len(saved_chunk_ids)} chunks saved). I now understand its structure and purpose."
        
    except Exception as e:
        logger.error(f"Failed to catalog Hugging Face Repo {repo_id}: {e}")
        return f"Error cataloging repository '{repo_id}': {str(e)}"


def deploy_model_to_ollama(model_name: str) -> str:
    """
    Deploy a Hugging Face model directly to Ollama.
    
    Args:
        model_name: The Hugging Face model string (e.g., 'hf.co/Qwen/Qwen2.5-32B-Instruct-GGUF' or just 'Qwen/Qwen2.5-32B-Instruct-GGUF').
        
    Returns:
        Status string.
    """
    try:
        # Ensure it starts with hf.co/ if it's a Hugging Face repo
        if not model_name.startswith("hf.co/") and "/" in model_name:
            model_name = f"hf.co/{model_name}"
            
        logger.info(f"Initiating ollama pull for {model_name}...")
        
        # We start the pull in the background so we don't block the agent for hours
        subprocess.Popen(
            ["ollama", "pull", model_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        
        return f"Successfully initiated background deployment of '{model_name}' to local Ollama nodes. It may take some time to download depending on the size."
    except Exception as e:
        logger.error(f"Failed to deploy model {model_name} to Ollama: {e}")
        return f"Error deploying model: {str(e)}"


# Global iterators for streaming datasets to avoid loading the whole thing in memory
_DATASET_ITERATORS = {}

def fetch_dataset_batch(repo_id: str, split: str = "train", batch_size: int = 5) -> str:
    """
    Stream a batch of rows from a Hugging Face dataset. Maintains state so subsequent
    calls return the next batch.
    
    Args:
        repo_id: The HF dataset repo ID (e.g., 'argilla/magpie-ultra-v0.1')
        split: The split to read ('train', 'test', 'validation')
        batch_size: Number of rows to return per batch
        
    Returns:
        JSON string of the dataset rows.
    """
    import json
    try:
        from datasets import load_dataset
    except ImportError:
        return "Error: The 'datasets' library is not installed. Run 'pip install datasets'."
        
    iterator_key = f"{repo_id}_{split}"
    
    try:
        if iterator_key not in _DATASET_ITERATORS:
            logger.info(f"Initializing dataset stream for {iterator_key}...")
            # Use streaming=True to prevent massive downloads blocking the thread
            ds = load_dataset(repo_id, split=split, streaming=True)
            _DATASET_ITERATORS[iterator_key] = iter(ds)
            
        iterator = _DATASET_ITERATORS[iterator_key]
        
        batch = []
        for _ in range(batch_size):
            try:
                row = next(iterator)
                batch.append(row)
            except StopIteration:
                # If we reach the end, clear the iterator
                del _DATASET_ITERATORS[iterator_key]
                if not batch:
                    return f"Reached the end of the dataset '{repo_id}' (split: {split})."
                break
                
        return json.dumps({"repo_id": repo_id, "split": split, "batch_size_returned": len(batch), "rows": batch}, indent=2)
        
    except Exception as e:
        logger.error(f"Failed to fetch dataset batch from {repo_id}: {e}")
        return f"Error fetching dataset batch: {str(e)}"


HUGGINGFACE_TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_huggingface_hub",
            "description": "Search the Hugging Face Hub API for datasets, models, or papers. Use this to discover external data sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search term (e.g., 'sysmon', 'pentesting dataset', 'llama')."
                    },
                    "repo_type": {
                        "type": "string",
                        "description": "Must be 'dataset', 'model', or 'paper'."
                    }
                },
                "required": ["query", "repo_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "catalog_huggingface_repo",
            "description": "Download a Hugging Face repository's documentation, synthesize it, and commit it to long-term RAG memory so you understand how to use it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {
                        "type": "string",
                        "description": "The exact Repository ID found from searching (e.g., 'CMCC-ROCK/Pentest')."
                    },
                    "repo_type": {
                        "type": "string",
                        "description": "Must be 'dataset' or 'model'."
                    }
                },
                "required": ["repo_id", "repo_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_model_to_ollama",
            "description": "Deploy a Hugging Face model directly to Sabrina's local Ollama architecture. Use this to pull new models for your distributed swarm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model_name": {
                        "type": "string",
                        "description": "The Hugging Face model name (e.g., 'hf.co/Qwen/Qwen2.5-32B-Instruct-GGUF')."
                    }
                },
                "required": ["model_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_dataset_batch",
            "description": "Stream a batch of rows directly from a Hugging Face dataset. This maintains a pointer, so calling it repeatedly on the same dataset yields the next batch. Use this to actively research bug bounty reports or training data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {
                        "type": "string",
                        "description": "The Hugging Face dataset repository ID (e.g., 'your-org/bug_bounty_reports')."
                    },
                    "split": {
                        "type": "string",
                        "description": "The dataset split to read from (default: 'train').",
                        "default": "train"
                    },
                    "batch_size": {
                        "type": "integer",
                        "description": "Number of rows to fetch in this batch. Keep under 10 to avoid overflowing context window.",
                        "default": 5
                    }
                },
                "required": ["repo_id"]
            }
        }
    }
]

def execute_hf_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Dispatcher to execute a Hugging Face tool call."""
    if tool_name == "search_huggingface_hub":
        query = arguments.get("query", "")
        repo_type = arguments.get("repo_type", "dataset")
        return search_huggingface_hub(query=query, repo_type=repo_type)
    elif tool_name == "catalog_huggingface_repo":
        repo_id = arguments.get("repo_id", "")
        repo_type = arguments.get("repo_type", "dataset")
        return catalog_huggingface_repo(repo_id=repo_id, repo_type=repo_type)
    elif tool_name == "deploy_model_to_ollama":
        model_name = arguments.get("model_name", "")
        return deploy_model_to_ollama(model_name=model_name)
    elif tool_name == "fetch_dataset_batch":
        repo_id = arguments.get("repo_id", "")
        split = arguments.get("split", "train")
        batch_size = arguments.get("batch_size", 5)
        return fetch_dataset_batch(repo_id=repo_id, split=split, batch_size=batch_size)
    else:
        return f"Unknown Hugging Face tool: {tool_name}"
