"""
Sabrina SecureBERT Triage Engine
================================
A high-throughput, parameterized pre-filter model interface for Windows EVTX and SIEM logs.
Applies confidence and top-2 margin thresholding to prevent false positives and
automatically route ambiguous or benign events to the LLM (Sabrina / CrewAI) for deep reasoning.
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("sabrina.securebert_triage")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class SecureBERTTriageEngine:
    """
    Confidence & Margin Gated Triage Engine wrapping SecureBERT.
    
    Attributes:
        min_confidence (float): Cutoff below which an event is escalated to UNKNOWN.
        margin_threshold (float): Minimum difference required between #1 and #2 class.
    """
    def __init__(self, 
                 model_path: str = "/home/ego/bot/securebert_classifier_14", 
                 min_confidence: float = 0.80, 
                 margin_threshold: float = 0.15,
                 device: Optional[str] = None):
        self.model_path = model_path
        self.min_confidence = min_confidence
        self.margin_threshold = margin_threshold
        self._model = None
        self._tokenizer = None
        self._device = device
        self.id2label: Dict[int, str] = {}
        
    def _lazy_init(self):
        if self._model is not None:
            return
            
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        
        if self._device is None:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            
        logger.info(f"Loading SecureBERT model from {self.model_path} onto {self._device}...")
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        self._model = AutoModelForSequenceClassification.from_pretrained(self.model_path).to(self._device)
        self._model.eval()
        
        # Load labels mapping
        labels_file = os.path.join(self.model_path, "labels.json")
        if os.path.exists(labels_file):
            with open(labels_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.id2label = {int(k): v for k, v in data.get("id2label", {}).items()}
        else:
            self.id2label = {int(k): v for k, v in self._model.config.id2label.items()}

    def _get_label(self, idx: int) -> str:
        return self.id2label.get(idx, f"tactic_{idx}")

    def triage(self, log_text: str, 
               min_confidence: Optional[float] = None, 
               margin_threshold: Optional[float] = None) -> Dict[str, Any]:
        """
        Evaluate a SIEM / EVTX log string.
        
        Returns:
            Dict containing decision ('MALICIOUS' vs 'UNKNOWN_ESCALATE'),
            confidence, margin, and escalation reasons.
        """
        self._lazy_init()
        import torch
        import torch.nn.functional as F
        
        min_conf = min_confidence if min_confidence is not None else self.min_confidence
        margin_th = margin_threshold if margin_threshold is not None else self.margin_threshold
        
        inputs = self._tokenizer([log_text], padding=True, truncation=True, max_length=512, return_tensors="pt").to(self._device)
        with torch.no_grad():
            logits = self._model(**inputs).logits
            probs = F.softmax(logits, dim=-1)[0]
            
        top_k_probs, top_k_indices = torch.topk(probs, 3)
        top1_prob = top_k_probs[0].item()
        top2_prob = top_k_probs[1].item()
        margin = top1_prob - top2_prob
        
        top1_idx = top_k_indices[0].item()
        top2_idx = top_k_indices[1].item()
        top1_label = self._get_label(top1_idx)
        top2_label = self._get_label(top2_idx)
        
        candidates = []
        for p, idx in zip(top_k_probs, top_k_indices):
            candidates.append({
                "tactic": self._get_label(idx.item()),
                "probability": round(p.item(), 4)
            })
            
        needs_escalation = False
        reasons = []
        
        if top1_prob < min_conf:
            needs_escalation = True
            reasons.append(f"Confidence {top1_prob*100:.1f}% is below threshold {min_conf*100:.1f}%")
            
        if margin < margin_th:
            needs_escalation = True
            reasons.append(f"Margin {margin*100:.1f}% is below threshold {margin_th*100:.1f}% (conflicted between '{top1_label}' and '{top2_label}')")
            
        if needs_escalation:
            decision = "UNKNOWN_ESCALATE"
            predicted_tactic = None
        else:
            decision = "MALICIOUS"
            predicted_tactic = top1_label
            
        return {
            "decision": decision,
            "predicted_tactic": predicted_tactic,
            "top_guess": top1_label,
            "confidence": round(top1_prob, 4),
            "margin": round(margin, 4),
            "needs_escalation": needs_escalation,
            "escalation_reason": " | ".join(reasons) if reasons else "High confidence classification",
            "candidates": candidates
        }
