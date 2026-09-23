"""capquery - capture and replay SQL results to speed up Django test suites.

The package is a pytest plugin: it records the SELECT statements a test sends to
postgres together with their results, and on the next run it answers those
SELECTs from the capture instead of the database.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
