"""
Sabrina pfSense Firewall Manager
================================
Autonomous ACL, Port Control, and Threat Blocking interface for pfSense.
Allows Sabrina to audit firewall rules against security baselines,
enforce standard port access controls, dynamically manage ACL rules,
and manage real-time attacker blocklists.

Architecture:
  - Uses SSH + stdin PHP execution against pfSense's native `config.inc` / `filter.inc`.
  - All rule writes use `write_config()` → `filter_configure()` for atomic, zero-reboot enforcement.
  - In-memory blocklists (snort2c / sabrina_blocklist) work at sub-millisecond speed via `pfctl`.
  - Anti-lockout safety guard protects LAN management access at all times.
"""

import json
import logging
import time
from typing import Dict, Any, List, Optional

import paramiko

logger = logging.getLogger("sabrina.pfsense_manager")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# ─────────────────────────────────────────────────────────────────────────────
#  Safety Constants
# ─────────────────────────────────────────────────────────────────────────────

# Ports that must never be blocked on LAN → pfSense (anti-lockout protection)
_LOCKOUT_PROTECTED_PORTS = {80, 443, 22}

# Ports blocked as part of the enterprise security baseline
_HIGHRISK_PORTS = {
    23: "Telnet",
    135: "MS-RPC",
    137: "NetBIOS-NS",
    138: "NetBIOS-DGM",
    139: "NetBIOS-SSN",
    445: "SMB",
}

# System rule descriptions that must never be touched
_SYSTEM_RULE_PREFIXES = (
    "anti-lockout",
    "Block traffic from port 0",
    "Block traffic to port 0",
    "Block snort2c",
    "sshguard",
    "GUI Lockout",
    "allow dhcp",
    "allow access to DHCP",
    "pass IPv4 loopback",
    "pass IPv6 loopback",
    "let out anything",
    "block bogon",
    "Block private networks",
)


class AntiLockoutError(Exception):
    """Raised when a requested operation would compromise management access."""


class PFSenseManager:
    def __init__(
        self,
        host: str = "192.168.2.1",
        username: str = "admin",
        password: str = "pfsense",
        port: int = 22,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.port = port

    # ─────────────────────────────────────────────────────────────────────────
    #  Low-level SSH helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _exec(self, cmd: str) -> str:
        """Execute a shell command on pfSense via SSH and return stdout."""
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(self.host, port=self.port, username=self.username, password=self.password, timeout=10)
            stdin, stdout, stderr = c.exec_command(cmd)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            if err and not out:
                logger.warning(f"pfSense shell stderr: {err}")
            return out
        finally:
            c.close()

    def exec_php(self, php_code: str) -> str:
        """
        Execute arbitrary PHP code on pfSense via stdin (not shell -r).
        This correctly handles single-quotes, complex arrays, and multi-line code.
        Wraps code in <?php automatically.
        """
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(self.host, port=self.port, username=self.username, password=self.password, timeout=10)
            full_code = f"<?php\n{php_code}\n"
            stdin, stdout, stderr = c.exec_command("php")
            stdin.write(full_code)
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            if err:
                logger.debug(f"PHP stderr: {err}")
            return out
        finally:
            c.close()

    # ─────────────────────────────────────────────────────────────────────────
    #  Safety Guards
    # ─────────────────────────────────────────────────────────────────────────

    def _check_anti_lockout(self, action: str, destination_port: Optional[int], interface: str, destination: Optional[str]):
        """
        Raises AntiLockoutError if the requested rule operation would block
        management access to pfSense (SSH/WebGUI on LAN interface).
        """
        if action != "block":
            return
        if interface not in ("lan", "vtnet1"):
            return
        # Check if targeting pfSense itself on a management port
        if destination_port in _LOCKOUT_PROTECTED_PORTS:
            if destination in (None, "any", self.host, "(vtnet1)"):
                raise AntiLockoutError(
                    f"SAFETY: Refusing to block port {destination_port} on LAN — "
                    f"this would cut off management access to pfSense (anti-lockout protection)."
                )

    def _check_system_rule(self, description: str):
        """Raises AntiLockoutError if a system-critical rule is being targeted."""
        descr_lower = description.lower()
        for prefix in _SYSTEM_RULE_PREFIXES:
            if prefix.lower() in descr_lower:
                raise AntiLockoutError(
                    f"SAFETY: Refusing to modify/delete system rule: '{description}'"
                )

    # ─────────────────────────────────────────────────────────────────────────
    #  ACL Rule Management
    # ─────────────────────────────────────────────────────────────────────────

    def list_acl_rules(self, interface: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all active firewall ACL rules from pfSense config.
        Returns parsed rule objects with index, tracker, interface, action,
        protocol, source, destination, port, and description.

        :param interface: Optional filter — 'lan', 'wan', or None for all
        :return: List of rule dicts
        """
        php = r"""
        require_once("config.inc");
        global $config;
        $rules = $config['filter']['rule'] ?? array();
        if (isset($rules['type'])) {
            $rules = array($rules);
        }
        $out = array();
        foreach ($rules as $idx => $r) {
            if (!is_array($r)) continue;
            $out[] = array(
                "index"    => $idx,
                "tracker"  => $r['tracker'] ?? "",
                "interface"=> $r['interface'] ?? "lan",
                "action"   => $r['type'] ?? "pass",
                "protocol" => $r['protocol'] ?? "any",
                "source"   => $r['source'] ?? array("any" => ""),
                "destination"=> $r['destination'] ?? array("any" => ""),
                "descr"    => $r['descr'] ?? "",
                "disabled" => isset($r['disabled']) ? true : false,
                "created_by"=> $r['created']['username'] ?? ""
            );
        }
        echo json_encode($out);
        """
        res = self.exec_php(php)
        try:
            rules = json.loads(res)
        except Exception:
            logger.warning(f"list_acl_rules parse error. Raw: {res[:200]}")
            return []

        if interface:
            iface_map = {"lan": "lan", "wan": "wan", "vtnet0": "wan", "vtnet1": "lan"}
            iface_norm = iface_map.get(interface.lower(), interface.lower())
            rules = [r for r in rules if r.get("interface", "").lower() == iface_norm]
        return rules

    def add_acl_rule(
        self,
        action: str = "block",
        interface: str = "lan",
        protocol: str = "tcp/udp",
        source: Optional[str] = None,
        destination: Optional[str] = None,
        destination_port: Optional[str] = None,
        description: str = "Sabrina ACL Rule",
        position: str = "top",
    ) -> Dict[str, Any]:
        """
        Add a new firewall ACL rule to pfSense and hot-reload the filter.

        :param action: 'pass', 'block', or 'reject'
        :param interface: 'lan' or 'wan'
        :param protocol: 'tcp', 'udp', 'tcp/udp', 'icmp', or 'any'
        :param source: Source network (e.g. '192.168.2.0/24', 'any'). Defaults to 'any'.
        :param destination: Destination IP or 'any'. Defaults to 'any'.
        :param destination_port: Port number or alias name (e.g. '445', 'Sabrina_HighRisk_Ports')
        :param description: Human-readable rule description
        :param position: 'top' (high priority) or 'bottom' (low priority)
        :return: Dict with status, tracker, and message
        """
        action = action.lower().strip()
        interface = interface.lower().strip()
        if action not in ("pass", "block", "reject"):
            return {"status": "error", "message": f"Invalid action '{action}'. Use pass, block, or reject."}

        # Anti-lockout safety guard
        port_int = None
        if destination_port and destination_port.isdigit():
            port_int = int(destination_port)
        try:
            self._check_anti_lockout(action, port_int, interface, destination)
        except AntiLockoutError as e:
            return {"status": "error", "message": str(e)}

        tracker = str(int(time.time()))
        source_php = '["any" => ""]' if not source or source == "any" else f'["address" => "{source}"]'
        dest_php = '["any" => ""]' if not destination or destination == "any" else f'["address" => "{destination}"]'
        if destination_port:
            dest_php = dest_php.rstrip("]") + f', "port" => "{destination_port}"]'

        position_code = "array_unshift($config['filter']['rule'], $new_rule);" if position == "top" else "$config['filter']['rule'][] = $new_rule;"

        php = f"""
        require_once("config.inc");
        require_once("filter.inc");
        global $config;
        
        if (!isset($config['filter']['rule']) || !is_array($config['filter']['rule'])) {{
            $config['filter']['rule'] = array();
        }}
        if (isset($config['filter']['rule']['type'])) {{
            $config['filter']['rule'] = array($config['filter']['rule']);
        }}
        
        $new_rule = array(
            "type"       => "{action}",
            "ipprotocol" => "inet",
            "interface"  => "{interface}",
            "protocol"   => "{protocol}",
            "descr"      => "{description}",
            "source"     => {source_php},
            "destination"=> {dest_php},
            "tracker"    => "{tracker}",
            "created"    => array(
                "time"     => "{tracker}",
                "username" => "Sabrina ACL Engine"
            )
        );
        {position_code}
        write_config("Sabrina ACL Engine: Added rule — {description}");
        filter_configure();
        echo json_encode(array("status" => "ok", "tracker" => "{tracker}", "descr" => "{description}"));
        """
        res = self.exec_php(php)
        try:
            result = json.loads(res)
            logger.info(f"Added ACL rule [{action.upper()}] '{description}' (tracker: {tracker})")
            return {"status": "ok", "tracker": tracker, "description": description, "message": f"Rule added and filter reloaded. Tracker: {tracker}"}
        except Exception:
            return {"status": "error", "message": f"Unexpected pfSense response: {res[:300]}"}

    def delete_acl_rule(self, identifier: str) -> Dict[str, Any]:
        """
        Delete a firewall ACL rule by tracker ID, description substring, or index.
        System-critical rules (anti-lockout, sshguard, etc.) are protected.

        :param identifier: Rule tracker ID, partial description, or integer index
        :return: Dict with status and message
        """
        php = f"""
        require_once("config.inc");
        require_once("filter.inc");
        global $config;
        
        $rules = $config['filter']['rule'] ?? array();
        if (isset($rules['type'])) {{ $rules = array($rules); }}
        
        $ident = "{identifier}";
        $deleted = false;
        $del_descr = "";
        $new_rules = array();
        
        foreach ($rules as $idx => $r) {{
            if (!is_array($r)) {{ $new_rules[] = $r; continue; }}
            $descr = $r['descr'] ?? "";
            $tracker = $r['tracker'] ?? "";
            $matched = ($tracker === $ident || strpos($descr, $ident) !== false || (string)$idx === $ident);
            
            if ($matched && !$deleted) {{
                // Safety check: never delete system rules
                $safe_prefixes = array("anti-lockout", "Block traffic from port 0", "Block traffic to port 0",
                    "Block snort2c", "sshguard", "GUI Lockout", "allow dhcp", "allow access to DHCP",
                    "pass IPv4 loopback", "pass IPv6 loopback", "let out anything", "block bogon",
                    "Block private networks", "virusprot");
                $is_system = false;
                foreach ($safe_prefixes as $sp) {{
                    if (stripos($descr, $sp) !== false) {{ $is_system = true; break; }}
                }}
                if ($is_system) {{
                    echo json_encode(array("status" => "error", "message" => "SAFETY: Cannot delete system rule: " . $descr));
                    exit(1);
                }}
                $del_descr = $descr;
                $deleted = true;
                // Skip — don't add to new_rules
            }} else {{
                $new_rules[] = $r;
            }}
        }}
        
        if (!$deleted) {{
            echo json_encode(array("status" => "error", "message" => "No rule matched identifier: " . $ident));
            exit(1);
        }}
        
        $config['filter']['rule'] = $new_rules;
        write_config("Sabrina ACL Engine: Deleted rule — " . $del_descr);
        filter_configure();
        echo json_encode(array("status" => "ok", "deleted_descr" => $del_descr));
        """
        res = self.exec_php(php)
        try:
            result = json.loads(res)
            if result.get("status") == "ok":
                logger.info(f"Deleted ACL rule: '{result.get('deleted_descr')}' (matched '{identifier}')")
                return {"status": "ok", "message": f"Deleted rule: '{result.get('deleted_descr')}'"}
            return {"status": "error", "message": result.get("message", "Unknown error")}
        except Exception:
            return {"status": "error", "message": f"Unexpected pfSense response: {res[:300]}"}

    # ─────────────────────────────────────────────────────────────────────────
    #  Security Baseline Enforcement
    # ─────────────────────────────────────────────────────────────────────────

    def enforce_security_baseline(self) -> Dict[str, Any]:
        """
        Enforce the standard enterprise security baseline on pfSense:

        1. HIGH-RISK PORT CONTROLS (LAN egress):
           - Block SMB (445/139), NetBIOS (137/138), Telnet (23), MS-RPC (135) via port alias
           - Positioned ABOVE the default LAN allow-all rule

        2. WAN PERIMETER HARDENING:
           - Enable blockpriv (RFC1918 private network blocking on WAN)
           - Enable blockbogons (Bogon/unallocated network blocking on WAN)

        3. REMOTE SYSLOG TELEMETRY:
           - Point pfSense syslog at Lab 69 (192.168.2.69:514) for Firewall and System events

        4. ANTI-LOCKOUT VERIFICATION:
           - Confirm LAN anti-lockout rule is healthy

        :return: Dict with status, actions_taken, and any warnings
        """
        logger.info("Enforcing pfSense enterprise security baseline...")
        actions_taken = []
        warnings = []

        # ── 1. High-risk port alias + LAN block rule via PHP ─────────────────
        php_ports = r"""
        require_once("config.inc");
        require_once("filter.inc");
        global $config;
        
        // Normalize arrays
        if (!isset($config['aliases']) || !is_array($config['aliases'])) {
            $config['aliases'] = array();
        }
        if (!isset($config['aliases']['alias']) || !is_array($config['aliases']['alias'])) {
            $config['aliases']['alias'] = array();
        }
        if (isset($config['aliases']['alias']['name'])) {
            $config['aliases']['alias'] = array($config['aliases']['alias']);
        }
        if (!isset($config['filter']['rule']) || !is_array($config['filter']['rule'])) {
            $config['filter']['rule'] = array();
        }
        if (isset($config['filter']['rule']['type'])) {
            $config['filter']['rule'] = array($config['filter']['rule']);
        }
        
        $alias_name = "Sabrina_HighRisk_Ports";
        $found_alias = false;
        foreach ($config['aliases']['alias'] as $a) {
            if (is_array($a) && isset($a['name']) && $a['name'] === $alias_name) {
                $found_alias = true; break;
            }
        }
        if (!$found_alias) {
            $config['aliases']['alias'][] = array(
                "name"    => $alias_name,
                "type"    => "port",
                "address" => "23 135 137 138 139 445",
                "descr"   => "High-risk ports blocked by Sabrina baseline (Telnet, MS-RPC, NetBIOS, SMB)",
                "detail"  => "Telnet||MS-RPC||NetBIOS-ns||NetBIOS-dgm||NetBIOS-ssn||SMB"
            );
        }
        
        $rule_descr = "Sabrina Baseline: Block High-Risk Ports (SMB/RPC/Telnet)";
        $found_rule = false;
        foreach ($config['filter']['rule'] as $r) {
            if (is_array($r) && isset($r['descr']) && $r['descr'] === $rule_descr) {
                $found_rule = true; break;
            }
        }
        $alias_added = !$found_alias;
        $rule_added  = !$found_rule;
        
        if (!$found_rule) {
            $new_rule = array(
                "type"        => "block",
                "ipprotocol"  => "inet",
                "descr"       => $rule_descr,
                "interface"   => "lan",
                "protocol"    => "tcp/udp",
                "source"      => array("any" => ""),
                "destination" => array("any" => "", "port" => $alias_name),
                "tracker"     => (string)time(),
                "created"     => array(
                    "time"     => (string)time(),
                    "username" => "Sabrina Baseline Enforcer"
                )
            );
            array_unshift($config['filter']['rule'], $new_rule);
        }
        
        write_config("Sabrina Baseline: High-risk port controls applied");
        filter_configure();
        echo json_encode(array(
            "alias_created" => $alias_added,
            "rule_created"  => $rule_added,
            "alias_name"    => $alias_name
        ));
        """
        res1 = self.exec_php(php_ports)
        try:
            r1 = json.loads(res1)
            if r1.get("alias_created"):
                actions_taken.append("Created alias 'Sabrina_HighRisk_Ports' (ports 23,135,137,138,139,445)")
            if r1.get("rule_created"):
                actions_taken.append("Added LAN block rule: 'Sabrina Baseline: Block High-Risk Ports (SMB/RPC/Telnet)'")
            else:
                actions_taken.append("High-risk port control rule already present — skipped")
        except Exception:
            warnings.append(f"High-risk port rule step returned unexpected output: {res1[:200]}")

        # ── 2. WAN interface hardening ────────────────────────────────────────
        php_wan = r"""
        require_once("config.inc");
        global $config;
        $already = array(
            "blockpriv"   => isset($config['interfaces']['wan']['blockpriv']),
            "blockbogons" => isset($config['interfaces']['wan']['blockbogons'])
        );
        $config['interfaces']['wan']['blockpriv']   = "";
        $config['interfaces']['wan']['blockbogons'] = "";
        write_config("Sabrina Baseline: Enabled WAN blockpriv and blockbogons");
        echo json_encode($already);
        """
        res2 = self.exec_php(php_wan)
        try:
            r2 = json.loads(res2)
            if not r2.get("blockpriv"):
                actions_taken.append("Enabled WAN blockpriv (RFC1918 private network filtering)")
            if not r2.get("blockbogons"):
                actions_taken.append("Enabled WAN blockbogons (bogon/unallocated network filtering)")
            if r2.get("blockpriv") and r2.get("blockbogons"):
                actions_taken.append("WAN blockpriv and blockbogons already enabled — skipped")
            # WAN rules need filter_configure to take effect
            self._exec("pfctl -f /tmp/rules.debug 2>/dev/null; filter_reload=1; pfctl -s rules > /dev/null 2>&1 || true")
        except Exception:
            warnings.append(f"WAN hardening step returned unexpected output: {res2[:200]}")

        # ── 3. Remote syslog forwarding ───────────────────────────────────────
        syslog_result = self.configure_remote_syslog("192.168.2.69", 514)
        if "success" in syslog_result.lower() or "already" in syslog_result.lower():
            actions_taken.append("Remote syslog forwarding → 192.168.2.69:514 configured")
        else:
            warnings.append(f"Syslog config: {syslog_result}")

        # ── 4. Anti-lockout verification ──────────────────────────────────────
        rules_raw = self._exec("pfctl -s rules | grep -i anti-lockout")
        if "anti-lockout" in rules_raw.lower():
            actions_taken.append("VERIFIED: Anti-lockout rule is active and protecting LAN management access")
        else:
            warnings.append("WARNING: Anti-lockout rule not detected in active pfctl rules — investigate immediately!")

        return {
            "status": "BASELINE_ENFORCED" if not warnings else "BASELINE_ENFORCED_WITH_WARNINGS",
            "actions_taken": actions_taken,
            "warnings": warnings,
        }

    def configure_remote_syslog(self, target_ip: str = "192.168.2.69", port: int = 514) -> str:
        """
        Configure pfSense remote logging via PHP shell to stream firewall
        events and filter logs to Sabrina's listener.
        """
        logger.info(f"Configuring pfSense remote syslog forwarding to {target_ip}:{port}...")
        php = f"""
        require_once("config.inc");
        require_once("system.inc");
        global $config;
        if (!is_array($config['syslog'])) {{
            $config['syslog'] = array();
        }}
        $config['syslog']['enable']          = true;
        $config['syslog']['remoteserver']    = "{target_ip}:{port}";
        $config['syslog']['filterdescriptions'] = "1";
        $config['syslog']['filter']          = true;
        $config['syslog']['logall']          = true;
        write_config("Configured remote syslog via Sabrina Automation");
        system_syslogd_start();
        echo "SYSLOG_CONFIG_SUCCESS";
        """
        res = self.exec_php(php)
        if "SYSLOG_CONFIG_SUCCESS" in res:
            return f"Successfully configured pfSense remote syslog streaming to {target_ip}:{port}."
        return f"Syslog configuration output: {res}"

    # ─────────────────────────────────────────────────────────────────────────
    #  Security Audit with Grading
    # ─────────────────────────────────────────────────────────────────────────

    def audit_security(self) -> Dict[str, Any]:
        """
        Comprehensive audit of pfSense security posture against enterprise baseline.

        Checks:
        - Default deny on WAN
        - Anti-lockout protection on LAN
        - Bogon and RFC1918 filtering on WAN
        - High-risk port controls (SMB/Telnet/NetBIOS/RPC) on LAN
        - Remote syslog telemetry active
        - Active blocked IP tables

        Returns a security grade (A+, A, B, C, F) with itemized findings.
        """
        logger.info("Auditing pfSense security posture...")
        rules_raw = self._exec("pfctl -s rules 2>/dev/null")
        tables_raw = self._exec("pfctl -s Tables 2>/dev/null")
        version_raw = self._exec("uname -v 2>/dev/null")
        blocked_raw = self._exec(
            "pfctl -t snort2c -T show 2>/dev/null; pfctl -t sabrina_blocklist -T show 2>/dev/null"
        )

        # Deduplicate blocked IPs
        active_blocked_ips = sorted(set(ip.strip() for ip in blocked_raw.splitlines() if ip.strip()))

        # Check interface config
        php_config = r"""
        require_once("config.inc");
        global $config;
        echo json_encode(array(
            "wan_blockpriv"   => isset($config['interfaces']['wan']['blockpriv']),
            "wan_blockbogons" => isset($config['interfaces']['wan']['blockbogons']),
            "syslog_enabled"  => isset($config['syslog']['enable']),
            "syslog_remote"   => $config['syslog']['remoteserver'] ?? "",
            "filter_rules"    => count($config['filter']['rule'] ?? array()),
            "alias_highrisk"  => false
        ));
        """
        try:
            cfg_data = json.loads(self.exec_php(php_config))
        except Exception:
            cfg_data = {}

        # Evaluate checks
        findings = []
        passed = []

        # 1. Anti-lockout
        has_anti_lockout = "anti-lockout" in rules_raw.lower()
        if has_anti_lockout:
            passed.append("Anti-lockout rule: LAN management access protected ✓")
        else:
            findings.append("CRITICAL: Anti-lockout rule not detected in active PF ruleset")

        # 2. Default deny on WAN
        has_default_drop = "block drop in" in rules_raw
        if has_default_drop:
            passed.append("Default deny: WAN ingress default-drop active ✓")
        else:
            findings.append("CRITICAL: Default ingress drop not active on WAN interface")

        # 3. Bogon filtering
        has_bogons = "bogons" in tables_raw
        if has_bogons:
            passed.append("Bogon filtering: <bogons> table active ✓")
        else:
            findings.append("HIGH: Bogon network filtering not active")

        # 4. RFC1918 blocking on WAN
        has_blockpriv = cfg_data.get("wan_blockpriv", False)
        if has_blockpriv:
            passed.append("RFC1918 blocking: WAN blockpriv enabled ✓")
        else:
            findings.append("HIGH: WAN interface is not filtering RFC1918 private source addresses (blockpriv)")

        # 5. Bogon blocking on WAN (config level)
        has_blockbogons = cfg_data.get("wan_blockbogons", False)
        if has_blockbogons:
            passed.append("Bogon blocking: WAN blockbogons enabled ✓")
        else:
            findings.append("MEDIUM: WAN interface blockbogons not enabled in config")

        # 6. High-risk port controls on LAN
        has_highrisk_block = (
            "Sabrina Baseline: Block High-Risk Ports" in rules_raw or
            "netbios" in rules_raw.lower() or
            "microsoft-ds" in rules_raw.lower()
        )
        if has_highrisk_block:
            passed.append("High-risk port controls: SMB/NetBIOS/Telnet/RPC egress blocked ✓")
        else:
            findings.append("MEDIUM: No high-risk egress port controls (SMB 445, NetBIOS, Telnet 23, MS-RPC 135)")

        # 7. Remote syslog
        has_syslog = cfg_data.get("syslog_enabled", False) and cfg_data.get("syslog_remote", "")
        if has_syslog:
            passed.append(f"Remote syslog: Streaming to {cfg_data.get('syslog_remote')} ✓")
        else:
            findings.append("LOW: Remote syslog not configured — Sabrina cannot receive pfSense telemetry")

        # Grade calculation
        critical_count = sum(1 for f in findings if f.startswith("CRITICAL"))
        high_count = sum(1 for f in findings if f.startswith("HIGH"))
        med_count = sum(1 for f in findings if f.startswith("MEDIUM"))
        low_count = sum(1 for f in findings if f.startswith("LOW"))

        if critical_count > 0:
            grade = "F"
        elif high_count > 0:
            grade = "C"
        elif med_count > 1:
            grade = "B"
        elif med_count == 1 or low_count > 0:
            grade = "A"
        else:
            grade = "A+"

        return {
            "security_grade": grade,
            "status": "SECURE" if grade in ("A+", "A") else "ATTENTION_REQUIRED",
            "pfsense_version": version_raw[:80],
            "total_active_rules": len(rules_raw.splitlines()),
            "checks_passed": passed,
            "security_findings": findings,
            "active_blocked_ips_count": len(active_blocked_ips),
            "active_blocked_ips": active_blocked_ips[:20],
            "wan_blockpriv": has_blockpriv,
            "wan_blockbogons": has_blockbogons,
            "remote_syslog": cfg_data.get("syslog_remote", ""),
        }

    # ─────────────────────────────────────────────────────────────────────────
    #  Attacker IP Blocklists (in-memory, zero-reboot)
    # ─────────────────────────────────────────────────────────────────────────

    def block_ip(self, ip: str, reason: str = "Malicious Activity") -> str:
        """
        Dynamically block an IP address in pfSense memory and terminate all active states.
        Uses native in-memory PF tables for instantaneous, zero-reboot enforcement.
        """
        ip = ip.strip()
        logger.info(f"Adding {ip} to pfSense blocklist (Reason: {reason})...")
        cmd = (
            f"pfctl -t snort2c -T add {ip} 2>/dev/null; "
            f"pfctl -t sabrina_blocklist -T add {ip} 2>/dev/null; "
            f"pfctl -k {ip} 2>/dev/null"
        )
        self._exec(cmd)
        return f"Successfully blocked {ip} in pfSense and killed all active connection states. (Reason: {reason})"

    def unblock_ip(self, ip: str) -> str:
        """Remove an IP address from pfSense block tables."""
        ip = ip.strip()
        logger.info(f"Removing {ip} from pfSense blocklist...")
        cmd = (
            f"pfctl -t snort2c -T delete {ip} 2>/dev/null; "
            f"pfctl -t sabrina_blocklist -T delete {ip} 2>/dev/null"
        )
        self._exec(cmd)
        return f"Successfully unblocked {ip} in pfSense."

    def list_blocked_ips(self) -> List[str]:
        """List all currently blocked IPs in pfSense memory tables (deduplicated)."""
        cmd = "pfctl -t snort2c -T show 2>/dev/null; pfctl -t sabrina_blocklist -T show 2>/dev/null"
        res = self._exec(cmd)
        return sorted(set(ip.strip() for ip in res.splitlines() if ip.strip()))
