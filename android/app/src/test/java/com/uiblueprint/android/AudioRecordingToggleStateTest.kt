package com.uiblueprint.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class AudioRecordingToggleStateTest {

    private lateinit var state: AudioRecordingToggleState

    @Before
    fun setUp() {
        state = AudioRecordingToggleState(
            labelRecord = "Record Audio 20 s",
            labelStop = "Stop Audio",
        )
    }

    // -------------------------------------------------------------------------
    // Initial state
    // -------------------------------------------------------------------------

    @Test
    fun `initial state is not recording`() {
        assertFalse(state.isRecording)
    }

    @Test
    fun `initial button label is record label`() {
        assertEquals("Record Audio 20 s", state.buttonLabel)
    }

    @Test
    fun `button is always enabled`() {
        assertTrue(state.isButtonEnabled)
    }

    // -------------------------------------------------------------------------
    // Start recording
    // -------------------------------------------------------------------------

    @Test
    fun `onRecordingStarted transitions to recording`() {
        val result = state.onRecordingStarted()

        assertTrue(result)
        assertTrue(state.isRecording)
    }

    @Test
    fun `button label becomes stop label while recording`() {
        state.onRecordingStarted()

        assertEquals("Stop Audio", state.buttonLabel)
    }

    @Test
    fun `button remains enabled while recording`() {
        state.onRecordingStarted()

        assertTrue(state.isButtonEnabled)
    }

    @Test
    fun `onRecordingStarted is a no-op when already recording`() {
        state.onRecordingStarted()
        val result = state.onRecordingStarted()

        assertFalse(result)
        assertTrue(state.isRecording)
    }

    // -------------------------------------------------------------------------
    // Stop recording
    // -------------------------------------------------------------------------

    @Test
    fun `onRecordingStopped transitions back to idle`() {
        state.onRecordingStarted()
        val result = state.onRecordingStopped()

        assertTrue(result)
        assertFalse(state.isRecording)
    }

    @Test
    fun `button label resets to record label after stop`() {
        state.onRecordingStarted()
        state.onRecordingStopped()

        assertEquals("Record Audio 20 s", state.buttonLabel)
    }

    @Test
    fun `onRecordingStopped is a no-op when not recording`() {
        val result = state.onRecordingStopped()

        assertFalse(result)
        assertFalse(state.isRecording)
    }

    // -------------------------------------------------------------------------
    // Start failure
    // -------------------------------------------------------------------------

    @Test
    fun `onStartFailed resets to idle from tentative recording state`() {
        state.onRecordingStarted()
        state.onStartFailed()

        assertFalse(state.isRecording)
        assertEquals("Record Audio 20 s", state.buttonLabel)
    }

    @Test
    fun `onStartFailed is a no-op when already idle`() {
        state.onStartFailed()

        assertFalse(state.isRecording)
        assertEquals("Record Audio 20 s", state.buttonLabel)
    }

    // -------------------------------------------------------------------------
    // Full toggle cycle
    // -------------------------------------------------------------------------

    @Test
    fun `full start-stop-start cycle works correctly`() {
        // First recording session
        state.onRecordingStarted()
        assertTrue(state.isRecording)
        assertEquals("Stop Audio", state.buttonLabel)

        state.onRecordingStopped()
        assertFalse(state.isRecording)
        assertEquals("Record Audio 20 s", state.buttonLabel)

        // Second recording session (button is still enabled)
        assertTrue(state.isButtonEnabled)
        state.onRecordingStarted()
        assertTrue(state.isRecording)
        assertEquals("Stop Audio", state.buttonLabel)
    }

    @Test
    fun `auto-stop after timeout behaves same as manual stop`() {
        state.onRecordingStarted()
        // Service posts ACTION_AUDIO_CAPTURE_DONE after the max-duration timer fires.
        // The activity calls onRecordingStopped() in the broadcast receiver.
        state.onRecordingStopped()

        assertFalse(state.isRecording)
        assertEquals("Record Audio 20 s", state.buttonLabel)
        assertTrue(state.isButtonEnabled)
    }
}
