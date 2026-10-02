"""Shared response clock for the board, queue and escalation policy.

Delivery confirms a reply, but its creation time is the conversation boundary:
a customer message received while the reply was queued still needs an answer.
Legacy messages predate the outbox and remain eligible as human replies.
"""

WAITING_SINCE_SQL = """(
    SELECT MIN(m.created_at) FROM messages m
    WHERE m.ticket_id = t.id AND m.sender_type = 'user' AND m.is_internal = false
      AND m.created_at > COALESCE((
          SELECT MAX(a.created_at) FROM messages a
          WHERE a.ticket_id = t.id AND a.sender_type = 'admin'
            AND a.is_internal = false AND a.delivery_status = 'delivered'
            AND (a.operator_id IS NOT NULL OR a.sender_id <> 0 OR NOT EXISTS (
                SELECT 1 FROM outbound_jobs oj WHERE oj.message_id = a.id
            ))
      ), '1970-01-01')
)"""
