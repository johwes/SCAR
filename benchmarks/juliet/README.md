# Juliet Test Suite

The [Juliet Test Suite for C/C++](https://samate.nist.gov/SARD/test-suites/116)
(NIST/NSA) is the primary benchmark for SCAR validation.

## Setup

```bash
# Download from NIST SARD
curl -O https://samate.nist.gov/SARD/downloads/juliet-test-suite-for-c-cplusplus-v1-3.zip
unzip juliet-test-suite-for-c-cplusplus-v1-3.zip -d juliet/
```

## Relevant CWE Categories

Focus on memory safety classes that IKOS covers:

| CWE | Description |
|-----|-------------|
| CWE-121 | Stack-based buffer overflow |
| CWE-122 | Heap-based buffer overflow |
| CWE-476 | NULL pointer dereference |
| CWE-190 | Integer overflow |
| CWE-415 | Double free |

## Running SCAR Against Juliet

```bash
# Example: scan CWE-121 test cases
tkn pipeline start scar \
  --param repo-url=file://$(pwd)/juliet/C/testcases/CWE121_Stack_Based_Buffer_Overflow \
  --workspace name=shared-data,claimName=scar-pvc
```
