# JSON Parser — Project Specification

> **Purpose**: Benchmark for correctness-driven experiment. The spec is deliberately
> vague on edge cases — the LLM must figure out the details on its own.

## Overview

Build a JSON parser from scratch in Python. No `json` module, no third-party
libraries. The parser reads a string and returns a Python object (dict, list,
str, int, float, bool, None).

Single file `parser.py` with a `parse(text: str) -> object` function.

## Feature List

### F1: Lexer

Tokenize JSON text into a stream of tokens.

Tokens: `{`, `}`, `[`, `]`, `:`, `,`, STRING, NUMBER, TRUE, FALSE, NULL.

### F2: Parse Basic Values

`parse("42")` → `42`, `parse("true")` → `True`, `parse(`"hello"`)` → `"hello"`, `parse("null")` → `None`.

Number must support: integers, negative numbers, floating point.

### F3: Parse Arrays

`parse("[1, 2, 3]")` → `[1, 2, 3]`. Support nested arrays, empty arrays.

### F4: Parse Objects

`parse('{"a": 1, "b": [2, 3]}')` → `{"a": 1, "b": [2, 3]}`. Support nested
objects, empty objects, arbitrary nesting depth.

### F5: String Escapes

Handle standard JSON escape sequences: `\"`, `\\`, `\/`, `\n`, `\t`, `\r`, `\b`, `\f`.

Also handle `\uXXXX` unicode escapes.

### F6: Error Handling

Malformed JSON must raise descriptive errors. Include the position or context
of the error.

### F7: CLI

```
python parser.py file.json     # parse file and print result
python parser.py -              # parse from stdin
```

Print result as valid JSON (using Python's `print` with `repr` or similar is fine).

## What We Care About

- Correctness — `parse(inverse(parse(x)))` should work for any JSON value
- Clear error messages
- Clean, readable code

## Test Suite

A separate test suite `test_suite.py` will validate the parser against ~30
test cases including:
- Valid JSON of all types
- Edge cases (empty containers, unicode, number formats)
- Malformed JSON (syntax errors, unclosed brackets, bad escapes)
- Stress cases (deep nesting, long strings)

The test suite is NOT provided to the LLM during implementation.
