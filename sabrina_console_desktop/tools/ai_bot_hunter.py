#!/usr/bin/env python3
"""
tools/ai_bot_hunter.py
======================
Discovers, benchmarks, and catalogs free web-based AI chatbots.
Stores metrics (latency, speed, capabilities) in the central Sabrina Redis Database.
Avoids static prompts by asking Sabrina to generate dynamic "Promptbreeder" style evaluations.
"""

import os
import sys
import time
import json
import hashlib
import redis
import re

# Add parent directory to path to import orchestrator
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from sabrina_coven_orchestrator_LAB import ROUTER
except ImportError:
    ROUTER = None
    print("[!] Warning: Could not import MultiNodeLLMRouter from sabrina_coven_orchestrator_LAB.")

SEED_URLS = [
    "https://duck.ai",
    "https://huggingface.co/chat",
    "https://chat.mistral.ai",
    "https://labs.perplexity.ai",
    "https://you.com/chat",
    "https://chat.deepseek.com",
    "https://gemini.google.com/app",
    "https://copilot.microsoft.com",
    "https://poe.com",
    "https://groq.com",
]

class AIBotHunter:
    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or os.environ.get("SABRINA_REDIS_URL", "redis://192.168.2.69:6379/0")
        self.client = None
        self._connect_redis()
        
    def _connect_redis(self):
        try:
            self.client = redis.from_url(self.redis_url, decode_responses=True, socket_timeout=3)
            self.client.ping()
            print(f"[+] Connected to Redis at {self.redis_url}")
        except Exception as e:
            print(f"[-] Redis connection failed: {e}. Benchmarks will not be saved.")
            self.client = None

    def _generate_dynamic_prompt(self) -> str:
        """
        Uses Sabrina's MultiNodeLLMRouter to dynamically generate a prompt
        designed to test coding and reasoning capabilities, simulating the Promptbreeder concept.
        """
        if not ROUTER:
            return "Write a python function to reverse a string."
            
        system = "You are a prompt engineer testing a new language model. Generate a short, complex prompt that tests both reasoning and coding ability. Output ONLY the prompt."
        user = "Generate a single benchmark prompt now."
        
        try:
            prompt = ROUTER.route(system=system, user=user, task_type="planner", max_tokens=256)
            if prompt:
                return prompt.strip()
        except Exception as e:
            print(f"[-] Error generating dynamic prompt: {e}")
            
        return "Write a python function that implements a simple LRU cache."

    def _evaluate_response(self, response_text: str) -> dict:
        """
        Uses Sabrina to evaluate the response from the hunted AI bot.
        """
        if not ROUTER or not response_text:
            return {"score": 0, "has_code": False, "capabilities": ["unknown"]}
            
        system = """You are an AI capabilities evaluator. Analyze the given text and output a JSON object with:
- "score": an integer 1-10 on reasoning quality
- "has_code": boolean if code is present
- "capabilities": list of strings (e.g. ["coding", "logic", "explanation"])
Output ONLY valid JSON."""
        
        try:
            eval_res = ROUTER.route(system=system, user=response_text[:4000], task_type="planner", max_tokens=256)
            
            # Clean up potential markdown formatting
            if eval_res:
                eval_res = eval_res.replace("```json", "").replace("```", "").strip()
                return json.loads(eval_res)
        except Exception as e:
            print(f"[-] Error evaluating response: {e}")
            
        return {"score": 5, "has_code": "```" in response_text, "capabilities": ["unknown"]}

    def benchmark_url(self, url: str) -> dict:
        """
        Uses Playwright to navigate to the URL, find the chat interface,
        submit a dynamic prompt, and measure latency and speed.
        """
        print(f"\n[*] Benchmarking {url}...")
        metrics = {
            "url": url,
            "status": "failed",
            "latency_ms": 0,
            "speed_cps": 0.0, # characters per second
            "evaluation": {}
        }
        
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("[-] Playwright not installed. Run: pip install playwright")
            return metrics

        prompt = self._generate_dynamic_prompt()
        print(f"  [>] Using dynamic prompt: {prompt[:60]}...")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                )
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                
                # Setup stealth
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                
                start_nav = time.time()
                page.goto(url, timeout=30000)
                page.wait_for_load_state("domcontentloaded")
                
                # Check for "Continue" / "Accept" modal (e.g. duck.ai disclaimer)
                try:
                    continue_btn = page.get_by_role("button", name=re.compile("continue|accept|agree", re.IGNORECASE)).first
                    if continue_btn.is_visible(timeout=2000):
                        continue_btn.click()
                        page.wait_for_timeout(1000)
                except Exception:
                    pass
                
                # Heuristic: Find chat input
                # Try common selectors for AI chats
                selectors = [
                    'textarea[placeholder*="message" i]',
                    'textarea[placeholder*="prompt" i]',
                    'textarea[placeholder*="ask" i]',
                    'textarea[placeholder*="search" i]',
                    '[contenteditable="true"]',
                    'input[type="text"]'
                ]
                
                input_locator = None
                for selector in selectors:
                    try:
                        loc = page.locator(selector).first
                        if loc.is_visible(timeout=2000):
                            input_locator = loc
                            break
                    except Exception:
                        continue
                        
                if not input_locator:
                    print(f"  [-] Could not locate chat input field on {url}")
                    browser.close()
                    return metrics
                    
                # Fill prompt and submit (Enter key)
                input_locator.fill(prompt)
                
                t_submit = time.time()
                input_locator.press("Enter")
                
                # Heuristic: Wait for network activity or DOM changes indicating a response
                # This is tricky without knowing the exact output selector.
                # We'll wait a bit, then grab all text, wait a bit more, and see if it changed.
                
                page.wait_for_timeout(3000) # Give it 3s to start answering
                t_first_response = time.time()
                latency_ms = int((t_first_response - t_submit) * 1000)
                
                # Wait for stream to finish (approximate by waiting for DOM to stabilize)
                # We wait 15 seconds max for the answer to generate
                page.wait_for_timeout(10000)
                t_finish = time.time()
                
                body_text = page.inner_text("body")
                browser.close()
                
                if len(body_text) > 100:
                    speed_cps = len(body_text) / (t_finish - t_submit)
                    metrics["latency_ms"] = latency_ms
                    metrics["speed_cps"] = round(speed_cps, 2)
                    metrics["status"] = "success"
                    
                    # Evaluate the output quality (we pass a snippet of the body to avoid context limits)
                    print("  [*] Evaluating response capabilities...")
                    eval_data = self._evaluate_response(body_text[:2000])
                    metrics["evaluation"] = eval_data
                    
                    print(f"  [+] Success! Latency: {latency_ms}ms, Speed: {speed_cps:.2f} cps, Score: {eval_data.get('score')}")
                else:
                    print("  [-] Response text too short, considering failed.")

        except Exception as e:
            print(f"  [-] Playwright error: {e}")
            
        return metrics

    def catalog_bot(self, metrics: dict):
        """Save the metrics to Redis hash set."""
        if not self.client or metrics["status"] != "success":
            return
            
        url = metrics["url"]
        url_hash = hashlib.md5(url.encode()).hexdigest()
        redis_key = f"sabrina:oracle_catalog:{url_hash}"
        
        # Flatten for redis hash
        data = {
            "url": url,
            "latency_ms": str(metrics["latency_ms"]),
            "speed_cps": str(metrics["speed_cps"]),
            "score": str(metrics["evaluation"].get("score", 0)),
            "has_code": str(metrics["evaluation"].get("has_code", False)),
            "capabilities": ",".join(metrics["evaluation"].get("capabilities", [])),
            "last_tested": str(int(time.time()))
        }
        
        try:
            self.client.hset(redis_key, mapping=data)
            # Add to a set of all cataloged oracles
            self.client.sadd("sabrina:oracle_set", redis_key)
            print(f"  [+] Cataloged {url} into Redis ({redis_key})")
        except Exception as e:
            print(f"  [-] Failed to catalog into Redis: {e}")

    def run(self):
        print(f"[=] Starting AI Bot Hunter targeting {len(SEED_URLS)} seeds...")
        for url in SEED_URLS:
            metrics = self.benchmark_url(url)
            self.catalog_bot(metrics)
        print("[=] Bot Hunter finished.")

if __name__ == "__main__":
    hunter = AIBotHunter()
    hunter.run()
