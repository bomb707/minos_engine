# Layer 1 Known Limitations

- **Composite scorer out of scope.** Layer 1 is descriptive; it never computes the
  Minos AdvancedScorer, TP/FP/FN, hap.py scores, predicted reward, or GATK params.
- **Calibration deferred (INFERRED).** `pileup.max_depth`, the quantile algorithm
  identity, the cost-model coefficients, and the confidence/risk transforms use
  conservative, documented, versioned defaults (`layer1-profiler-v1`); calibration on
  the development BAMs is a later step. Difficulty is descriptive and monotonic and
  never encodes a GATK recommendation.
- **Parquet byte-stability is environment-pinned.** Window content identity is the
  canonical-JSON fingerprint; the Parquet file is byte-stable under the pinned
  `pyarrow` but not guaranteed identical across pyarrow versions (see DETERMINISM).
- **samtools parity is diagnostic only.** Core coverage/pileup use pysam; a samtools
  cross-check would be an optional integration check, never a CI dependency.
- **hap.py normalization not reproduced.** Layer 1 does not run hap.py and does not
  reproduce its normalization; evidence proxies are raw-BAM measurements only.
- **Reference annotations.** Repeat-mask / mappability tracks are not consumed;
  FASTA-derived indicators are never labelled as such.
