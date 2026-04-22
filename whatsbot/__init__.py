"""whatsbot — persönlicher WhatsApp-Bot zur Fernsteuerung von Claude Code auf macOS.

Hexagonal architecture:
    domain/      pure logic, keine I/O
    ports/       Protocol/ABC-Interfaces
    adapters/    konkrete I/O-Implementierungen
    application/ Use-Cases, orchestriert Ports
    http/        FastAPI-Transport-Layer

Single-User, Max-20x-Subscription only. NIE claude-agent-sdk.
"""

__version__ = "0.1.0"
