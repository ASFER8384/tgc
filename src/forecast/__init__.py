"""Demand forecast.

What will sell, how many, and who will buy it — learned from the purchase history
the customer platform already holds, and written back as the number the buying
desk orders against.

The module is built around one refusal: it will not publish a forecast it cannot
show is better than the eight-week average already running. Training happens on
weeks it is then not allowed to see, the trained model and the incumbent are
scored on the same held-back weeks, and if the model loses, nothing is published
and the average keeps running. That rule lives in ``evaluate.py`` and is enforced
in ``service.py``, not in a document.
"""
