import dataclasses
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
import re
import urllib.parse

@dataclass
class AuthContext:
    name: str
    token: str = ""
    headers: dict = field(default_factory=dict)

    def get_headers(self) -> dict:
        if self.token:
            return {**self.headers, "Authorization": f"Bearer {self.token}"}
        return self.headers

@dataclass
class IDORTarget:
    url: str
    path_pattern: str
    id_param: str
    owner_id: str
    test_ids: List[str]
    method: str = "GET"

class AccessControlProber:
    def __init__(self, base_url: str, contexts: Dict[str, AuthContext] = None):
        self.base_url = base_url
        self.contexts = contexts if contexts else {}

    def extract_numeric_targets(self, path: str) -> List[dict]:
        pattern = re.compile(r"/\w+/\d+")
        matches = pattern.findall(path)
        targets = []
        for match in matches:
            param = match.split("/")[-2]
            sample_id = match.split("/")[-1]
            target_info = {"pattern": f"{path.replace(match, '{id}')}",
                           "param": param,
                           "sample_id": sample_id}
            targets.append(target_info)
        return targets

    def evaluate_idor(self, owner_status: int, owner_body: str, attacker_status: int, attacker_body: str, anon_status: int, anon_body: str) -> Tuple[str, float, str]:
        if owner_status not in (200, 201):
            return ("INCONCLUSIVE", 0.0, "Owner request did not succeed.")
        
        if attacker_status in (401, 403) and anon_status in (401, 403):
            return ("SECURE", 1.0, "Access control properly enforced (401/403).")
        
        if attacker_status == 200 and attacker_body != "" and attacker_body == owner_body:
            return ("CONFIRMED_IDOR", 0.95, "Attacker accessed owner resource with identical response data.")
        
        if attacker_status == 200 and "error" not in attacker_body.lower() and len(attacker_body) > 20:
            return ("CONFIRMED_IDOR", 0.85, "Attacker successfully retrieved data with HTTP 200.")
        
        if attacker_status in (404, 500):
            return ("FALSE_POSITIVE", 0.9, "Resource not found or server error.")
        
        return ("NEEDS_REVIEW", 0.5, "Status code anomaly.")

if __name__ == '__main__':
    # Test 1
    target = IDORTarget(url="http://example.com/api/Users/42", path_pattern="/api/Users/{id}", id_param="id", owner_id="1", test_ids=["42"])
    prober = AccessControlProber(base_url=target.url)
    assert prober.extract_numeric_targets(target.path_pattern) == [{"pattern": "/api/Users/{id}", "param": "id", "sample_id": "42"}]
    
    # Test 2
    target = IDORTarget(url="http://example.com/rest/basket/9", path_pattern="/rest/basket/{id}", id_param="id", owner_id="1", test_ids=["9"])
    prober = AccessControlProber(base_url=target.url)
    assert prober.extract_numeric_targets(target.path_pattern) == [{"pattern": "/rest/basket/{id}", "param": "id", "sample_id": "9"}]
    
    # Test 3
    verdict, confidence, reason = prober.evaluate_idor(owner_status=200, owner_body="owner_data", attacker_status=403, attacker_body="", anon_status=401, anon_body="")
    assert (verdict, confidence, reason) == ("SECURE", 1.0, "Access control properly enforced (401/403).")
    
    # Test 4
    verdict, confidence, reason = prober.evaluate_idor(owner_status=200, owner_body="owner_data", attacker_status=200, attacker_body="owner_data", anon_status=401, anon_body="")
    assert (verdict, confidence, reason) == ("CONFIRMED_IDOR", 0.95, "Attacker accessed owner resource with identical response data.")
    
    # Test 5
    verdict, confidence, reason = prober.evaluate_idor(owner_status=200, owner_body="owner_data", attacker_status=500, attacker_body="", anon_status=401, anon_body="")
    assert (verdict, confidence, reason) == ("FALSE_POSITIVE", 0.9, "Resource not found or server error.")
    
    print("[SELF-TEST] ALL TESTS PASSED")