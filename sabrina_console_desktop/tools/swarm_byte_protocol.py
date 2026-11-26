"""Binary Instruction Set Architecture (ISA) & Wire Protocol for Sabrina's 1,000-Mind Collective.

Delineates instructions across 1,000+ heterogeneous agents using:
1. 8-Byte Binary Control Frames (`struct` big-endian `>BBBBHH`) for O(1) routing & sharding.
2. Orthogonal Coordinate Delineation (Platoon Module Shard x Squad x Role Opcode x Constraint Bitmask).
3. Token-Minimal Micro-DSL Projection for BPE-tokenized LLM interfaces.
4. Binary Result Packets (`>HBHHQI` + zlib-compressed AST payload) for zero-fluff consensus reduction.
"""
from __future__ import annotations

import ast
import binascii
import hashlib
import struct
import time
import zlib
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Any


class AgentRoleOpcode(IntEnum):
    """1-Byte Role Opcode delineating what each agent in a Squad executes."""
    SOLVE_SPEED = 0x01       # Optimize for O(1) latency / fast path
    SOLVE_MEMORY = 0x02      # Optimize for zero-allocation / bounded memory
    ADVERSARIAL_FUZZ = 0x03  # Generate NaN, Inf, clock-drift & race-condition exploits
    AUDIT_INVARIANT = 0x04   # Static AST & semantic boundary verification
    SQUAD_REDUCER = 0x05     # MapReduce aggregator for the 10-agent squad


class ConstraintMask(IntFlag):
    """2-Byte (uint16) Bitmask Register toggling strict engineering invariants."""
    NONE = 0x0000
    IEEE754_NAN_GUARD = 1 << 0   # 0x0001: Reject float('nan') / float('inf')
    THREAD_SAFE_LOCK = 1 << 1    # 0x0002: Enforce mutex / lock-free safety
    MONOTONIC_CLOCK = 1 << 2     # 0x0004: Require time.monotonic(), ban time.time()
    ZERO_EXTERNAL_DEPS = 1 << 3  # 0x0008: Standard library only
    STRICT_TYPE_HINTS = 1 << 4   # 0x0010: Full PEP-484 annotations
    AST_ONLY_OUTPUT = 1 << 5     # 0x0020: Zero prose; emit parseable code/frame only
    BOUNDED_MEMORY = 1 << 6      # 0x0040: No unbounded lists/caches
    DETERMINISTIC_SEED = 1 << 7  # 0x0080: Reproducible execution


# 8-Byte Instruction Frame Layout:
# Byte 0: version_and_tier (upper 4 bits = protocol version 1, lower 4 bits = tree tier 0..3)
# Byte 1: platoon_id (0..255) -> Horizontal Module / Subsystem Shard
# Byte 2: squad_id (0..255)   -> Sub-problem / Function Shard
# Byte 3: role_opcode         -> Vertical Role Specialization (AgentRoleOpcode)
# Bytes 4-5: constraint_mask  -> uint16 ConstraintMask bitfield
# Bytes 6-7: symbol_crc16     -> uint16 CRC16 hash of target symbol/function name
INSTRUCTION_STRUCT = struct.Struct(">BBBBHH")

# 19-Byte Binary Result Header Layout (+ variable compressed payload):
# Bytes 0-1: agent_id (uint16, 0..65535)
# Byte 2:    status_code (uint8: 1=OK, 2=QUORUM_CANCELLED, 3=BYZANTINE_FAULT)
# Bytes 3-4: passed_mask (uint16 ConstraintMask verified)
# Bytes 5-6: latency_ms (uint16)
# Bytes 7-14: ast_hash64 (uint64 structural fingerprint of normalized AST)
# Bytes 15-18: payload_len (uint32 length of zlib-compressed payload)
RESULT_HEADER_STRUCT = struct.Struct(">HBHHQI")


def crc16_symbol(symbol_name: str) -> int:
    """Compute a deterministic 16-bit CRC hash for a target function or module symbol."""
    return binascii.crc_hqx(symbol_name.encode("utf-8"), 0xFFFF) & 0xFFFF


def compute_ast_hash64(code_str: str) -> int:
    """Parse Python code into a normalized AST and return an 8-byte uint64 structural hash."""
    try:
        tree = ast.parse(code_str)
        normalized = ast.dump(tree, annotate_fields=True, include_attributes=False)
        digest = hashlib.sha256(normalized.encode("utf-8")).digest()
        return struct.unpack(">Q", digest[:8])[0]
    except Exception:
        return 0


@dataclass(frozen=True)
class ByteInstructionFrame:
    """8-Byte binary instruction descriptor for a single agent mind."""
    tree_tier: int
    platoon_id: int
    squad_id: int
    role: AgentRoleOpcode
    constraints: ConstraintMask
    symbol_crc16: int
    protocol_version: int = 1

    def to_bytes(self) -> bytes:
        """Pack the instruction into exactly 8 raw bytes."""
        ver_tier = ((self.protocol_version & 0x0F) << 4) | (self.tree_tier & 0x0F)
        return INSTRUCTION_STRUCT.pack(
            ver_tier,
            self.platoon_id & 0xFF,
            self.squad_id & 0xFF,
            int(self.role) & 0xFF,
            int(self.constraints) & 0xFFFF,
            self.symbol_crc16 & 0xFFFF,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> ByteInstructionFrame:
        """Unpack an 8-byte binary frame into a structured ByteInstructionFrame."""
        if len(raw) != INSTRUCTION_STRUCT.size:
            raise ValueError(f"Expected {INSTRUCTION_STRUCT.size} bytes, got {len(raw)}")
        ver_tier, platoon_id, squad_id, role_val, mask_val, sym_crc = INSTRUCTION_STRUCT.unpack(raw)
        version = (ver_tier >> 4) & 0x0F
        tier = ver_tier & 0x0F
        return cls(
            tree_tier=tier,
            platoon_id=platoon_id,
            squad_id=squad_id,
            role=AgentRoleOpcode(role_val),
            constraints=ConstraintMask(mask_val),
            symbol_crc16=sym_crc,
            protocol_version=version,
        )

    def to_micro_dsl(self, symbol_lookup: dict[int, str] | None = None) -> str:
        """Project the 8-byte frame into a token-aligned Micro-DSL header for BPE LLMs."""
        sym_label = (
            symbol_lookup.get(self.symbol_crc16, f"0x{self.symbol_crc16:04X}")
            if symbol_lookup
            else f"0x{self.symbol_crc16:04X}"
        )
        active_flags = [
            flag.name for flag in ConstraintMask if flag != ConstraintMask.NONE and (self.constraints & flag)
        ]
        flags_str = "|".join(active_flags) if active_flags else "NONE"
        return (
            f"@T{self.tree_tier}.P{self.platoon_id:02d}.S{self.squad_id:02d}"
            f"[OP:{self.role.name}|SYM:{sym_label}|MASK:0x{int(self.constraints):04X}({flags_str})]"
        )


@dataclass(frozen=True)
class BinaryResultPacket:
    """Binary result frame returned by an agent worker node."""
    agent_id: int
    status_code: int
    passed_mask: ConstraintMask
    latency_ms: int
    ast_hash64: int
    compressed_payload: bytes

    def to_bytes(self) -> bytes:
        header = RESULT_HEADER_STRUCT.pack(
            self.agent_id & 0xFFFF,
            self.status_code & 0xFF,
            int(self.passed_mask) & 0xFFFF,
            min(65535, max(0, self.latency_ms)),
            self.ast_hash64 & 0xFFFFFFFFFFFFFFFF,
            len(self.compressed_payload),
        )
        return header + self.compressed_payload

    @classmethod
    def from_code(
        cls,
        agent_id: int,
        status_code: int,
        passed_mask: ConstraintMask,
        latency_ms: int,
        code_str: str,
    ) -> BinaryResultPacket:
        ast_h = compute_ast_hash64(code_str) if code_str else 0
        comp = zlib.compress(code_str.encode("utf-8"), level=6) if code_str else b""
        return cls(
            agent_id=agent_id,
            status_code=status_code,
            passed_mask=passed_mask,
            latency_ms=latency_ms,
            ast_hash64=ast_h,
            compressed_payload=comp,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> BinaryResultPacket:
        if len(raw) < RESULT_HEADER_STRUCT.size:
            raise ValueError("Packet shorter than binary result header")
        agent_id, status, mask_val, lat, ast_h, plen = RESULT_HEADER_STRUCT.unpack(
            raw[: RESULT_HEADER_STRUCT.size]
        )
        payload = raw[RESULT_HEADER_STRUCT.size : RESULT_HEADER_STRUCT.size + plen]
        return cls(
            agent_id=agent_id,
            status_code=status,
            passed_mask=ConstraintMask(mask_val),
            latency_ms=lat,
            ast_hash64=ast_h,
            compressed_payload=payload,
        )

    def decompress_code(self) -> str:
        if not self.compressed_payload:
            return ""
        return zlib.decompress(self.compressed_payload).decode("utf-8")


def delineate_thousand_agent_dispatch_table(
    total_agents: int = 1000,
    squad_size: int = 10,
    platoon_size: int = 10,
    target_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Generate a complete 1,000-agent binary dispatch table and benchmark wire vs prose size."""
    t0 = time.time()
    symbols = target_symbols or [
        "TokenBucket.consume",
        "AgentDesktopBroker.execute_desktop_playbook",
        "CapabilityRegistry.weight_and_select_worker",
        "ExternalMindScout.benchmark_portal",
        "SabrinaSkillHydration.approve_proposal",
        "UnifiedOracleHub.query",
        "DurationParser.parse_duration",
        "AgentRuntime.step_round",
        "Blackboard.acquire_desktop_lease",
        "ContextSanitizer.redact_payload",
    ]
    symbol_map = {crc16_symbol(s): s for s in symbols}

    # Standard role distribution inside each 10-agent squad:
    # 0..3: SOLVE_SPEED, 4..5: SOLVE_MEMORY, 6..7: ADVERSARIAL_FUZZ, 8: AUDIT_INVARIANT, 9: SQUAD_REDUCER
    squad_role_schedule = [
        AgentRoleOpcode.SOLVE_SPEED,
        AgentRoleOpcode.SOLVE_SPEED,
        AgentRoleOpcode.SOLVE_SPEED,
        AgentRoleOpcode.SOLVE_SPEED,
        AgentRoleOpcode.SOLVE_MEMORY,
        AgentRoleOpcode.SOLVE_MEMORY,
        AgentRoleOpcode.ADVERSARIAL_FUZZ,
        AgentRoleOpcode.ADVERSARIAL_FUZZ,
        AgentRoleOpcode.AUDIT_INVARIANT,
        AgentRoleOpcode.SQUAD_REDUCER,
    ]

    default_mask = (
        ConstraintMask.IEEE754_NAN_GUARD
        | ConstraintMask.THREAD_SAFE_LOCK
        | ConstraintMask.MONOTONIC_CLOCK
        | ConstraintMask.ZERO_EXTERNAL_DEPS
        | ConstraintMask.AST_ONLY_OUTPUT
    )

    frames: list[ByteInstructionFrame] = []
    binary_stream = bytearray()
    prose_bytes_total = 0
    micro_dsl_bytes_total = 0

    for idx in range(total_agents):
        squad_global_idx = idx // squad_size
        platoon_id = (squad_global_idx // platoon_size) % len(symbols)
        squad_id = squad_global_idx % platoon_size
        slot_in_squad = idx % squad_size
        role = squad_role_schedule[slot_in_squad]
        sym_name = symbols[platoon_id]
        sym_crc = crc16_symbol(sym_name)

        frame = ByteInstructionFrame(
            tree_tier=1 if role == AgentRoleOpcode.SQUAD_REDUCER else 0,
            platoon_id=platoon_id,
            squad_id=squad_id,
            role=role,
            constraints=default_mask,
            symbol_crc16=sym_crc,
        )
        frames.append(frame)
        packed = frame.to_bytes()
        binary_stream.extend(packed)

        dsl_str = frame.to_micro_dsl(symbol_map)
        micro_dsl_bytes_total += len(dsl_str.encode("utf-8"))

        # Equivalent verbose English instruction paragraph
        verbose_prose = (
            f"Agent #{idx:04d}, you are assigned to Platoon {platoon_id} and Squad {squad_id}. "
            f"Your designated operational role is {role.name} on target symbol '{sym_name}'. "
            f"You must strictly enforce IEEE-754 NaN guards, thread-safe locking, monotonic clocks "
            f"(time.monotonic), zero external dependencies, and output pure parseable AST code only."
        )
        prose_bytes_total += len(verbose_prose.encode("utf-8"))

    compressed_table = zlib.compress(bytes(binary_stream), level=9)
    elapsed_ms = round((time.time() - t0) * 1000, 3)

    return {
        "ok": True,
        "total_agents": total_agents,
        "encode_elapsed_ms": elapsed_ms,
        "wire_metrics": {
            "verbose_english_bytes": prose_bytes_total,
            "micro_dsl_bytes": micro_dsl_bytes_total,
            "raw_binary_isa_bytes": len(binary_stream),
            "zlib_compressed_isa_bytes": len(compressed_table),
            "bytes_per_agent_raw": INSTRUCTION_STRUCT.size,
            "raw_binary_vs_prose_reduction": f"{round((1 - len(binary_stream) / prose_bytes_total) * 100, 2)}%",
            "compressed_vs_prose_reduction": f"{round((1 - len(compressed_table) / prose_bytes_total) * 100, 2)}%",
        },
        "sample_frames": [
            {
                "agent_id": i,
                "hex_8_bytes": frames[i].to_bytes().hex().upper(),
                "micro_dsl": frames[i].to_micro_dsl(symbol_map),
                "role": frames[i].role.name,
            }
            for i in (0, 4, 6, 8, 9, 100, 500)
        ],
    }
