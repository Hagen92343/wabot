"""Domain-Core: pure Logic, keine I/O. Zentrum der Hexagonal-Architektur.

Module hier dürfen NICHT importieren:
    - adapters/* (I/O-Implementierungen)
    - http/* (Transport)
    - keyring, sqlite3, requests, ... (externe I/O-Libs)

Erlaubt: dataclasses, typing, datetime, ulid, hashlib, re, ...
"""
