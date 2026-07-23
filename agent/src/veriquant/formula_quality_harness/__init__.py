def __getattr__(name: str):
    if name == "FormulaQualityHarness":
        from .harness import FormulaQualityHarness

        return FormulaQualityHarness
    raise AttributeError(name)

__all__ = ["FormulaQualityHarness"]
