← [Back to guide index](README.md) · [Main README](../../README.md)

# 🛠️ Development

## Contents
- [Git workflow](#git-workflow)
- [Running tests](#running-tests)

---

## Git workflow

All file changes are made on Windows, committed, pushed, then pulled on ZGX:

```bash
git -C ~/agno-hive pull   # on ZGX
```

**Never edit files directly on ZGX.**

---

## Running tests

```bash
pytest tests/ -v
```

---

← [Back to guide index](README.md)
