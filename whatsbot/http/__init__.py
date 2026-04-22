"""HTTP-Transport: FastAPI-Endpoints, Middleware, Webhook-Handler.

/webhook       Meta WhatsApp Webhook (POST + GET-Subscribe-Challenge)
/hook/bash     Pre-Tool-Hook IPC (localhost only, Shared-Secret)
/hook/write    Pre-Tool-Hook IPC (localhost only, Shared-Secret)
/health        Liveness probe
/metrics       Prometheus (localhost only, NICHT über Tunnel)
"""
