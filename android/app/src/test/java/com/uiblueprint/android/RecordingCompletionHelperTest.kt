package com.uiblueprint.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RecordingCompletionHelperTest {
    private val helper = RecordingCompletionHelper(timeoutMs = 15_000L)

    @Test
    fun `capture error normalizes to user-facing error`() {
        val event = helper.normalize(CaptureDoneEvent(error = "Capture failed"))

        assertEquals("Capture failed", event.error)
        assertEquals(null, event.clipPath)
    }

    @Test
    fun `capture success keeps clip path`() {
        val event = helper.normalize(CaptureDoneEvent(clipPath = "/tmp/capture_20260408.mp4"))

        assertEquals("/tmp/capture_20260408.mp4", event.clipPath)
        assertEquals(null, event.error)
    }

    @Test
    fun `missing extras normalize to fallback error`() {
        val event = helper.normalize(CaptureDoneEvent())

        assertEquals(CaptureDoneEvent.ERROR_NO_OUTPUT, event.error)
        assertEquals(null, event.clipPath)
    }

    @Test
    fun `watchdog timeout becomes true after fifteen seconds`() {
        assertFalse(helper.hasTimedOut(startedAtMs = 1_000L, nowMs = 15_999L))
        assertTrue(helper.hasTimedOut(startedAtMs = 1_000L, nowMs = 16_000L))
        assertEquals(1L, helper.remainingTimeoutMs(startedAtMs = 1_000L, nowMs = 15_999L))
    }
}
