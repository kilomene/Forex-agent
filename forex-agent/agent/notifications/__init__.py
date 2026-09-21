"""Agent notification sinks (Phase 6).

The agent event channel (localhost SSE, backed by the durable delivery
journal ``storage.event_journal``) is the PRIMARY notification path and
is always available. Worker, FCM, and webhook are OPTIONAL sinks.

Public surface:
  channels:   NotificationChannel, NotificationError, AgentChannel,
              WorkerChannel, FCMChannel, WebhookChannel
  dispatcher: NotificationDispatcher, build_dispatcher_from_config,
              run_dispatcher
"""

from agent.notifications.channels import (
    AgentChannel,
    FCMChannel,
    NotificationChannel,
    NotificationError,
    WebhookChannel,
    WorkerChannel,
)
from agent.notifications.dispatcher import (
    NotificationDispatcher,
    build_dispatcher_from_config,
    run_dispatcher,
)

__all__ = [
    "NotificationChannel",
    "NotificationError",
    "AgentChannel",
    "WorkerChannel",
    "FCMChannel",
    "WebhookChannel",
    "NotificationDispatcher",
    "build_dispatcher_from_config",
    "run_dispatcher",
]
