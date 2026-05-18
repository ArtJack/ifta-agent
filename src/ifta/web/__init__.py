"""Web intake layer — FastAPI app + SQLite jobs + worker.

The customer-facing form lives on artjeck.com and POSTs to the FastAPI app
in this package. The app accepts file uploads, saves them, and queues a job;
a separate worker process runs the existing IFTA pipeline + agent review
and emails the packet back to the customer.
"""
