"""External-tool adapters (ports) for the Validator Twin.

Stage 1 provides *side-effect-free* plan/parse adapters and deterministic fake
runners. No real GATK or hap.py process is executed. Runner ports are defined so
a later stage can supply a resource-capped container executor without changing
the contracts. Adapters never use ``shell=True``, never build a command as a
single shell string, and never log credentials or signed URLs.
"""
