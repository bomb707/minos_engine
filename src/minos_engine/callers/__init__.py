"""Callers — variant-caller execution interfaces and parameter registries.

The engine is GATK-only (ADR-0002): only ``callers/gatk`` is executable or
selectable through the active engine. Historical DeepVariant/BCFtools data may
be preserved elsewhere, but no adapter for them is provided here.
"""
