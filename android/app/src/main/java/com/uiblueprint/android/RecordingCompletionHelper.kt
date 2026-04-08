package com.uiblueprint.android

class RecordingCompletionHelper(
    private val timeoutMs: Long,
) {
    fun normalize(event: CaptureDoneEvent): CaptureDoneEvent = event.normalized()

    fun hasTimedOut(startedAtMs: Long, nowMs: Long): Boolean = nowMs - startedAtMs >= timeoutMs

    fun remainingTimeoutMs(startedAtMs: Long, nowMs: Long): Long =
        (timeoutMs - (nowMs - startedAtMs)).coerceAtLeast(0L)
}
