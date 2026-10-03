"""The worker process: scheduler, job queue, DB lease locks and heartbeat.

``runtime.Worker`` wires together ``queue``, ``locks``, ``heartbeat`` and ``execution``.
"""
