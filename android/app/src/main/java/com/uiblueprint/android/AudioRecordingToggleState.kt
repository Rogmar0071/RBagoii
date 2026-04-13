package com.uiblueprint.android

/**
 * Pure state machine for the "Record Audio 20 s" toggle button.
 *
 * Responsibilities:
 *  - Track whether audio recording is currently active.
 *  - Decide the button label that should be shown at each state.
 *  - Determine whether a new recording can be started / stopped.
 *
 * This class has no Android dependencies so it can be exercised by plain JVM
 * unit tests.
 */
class AudioRecordingToggleState(
    private val labelRecord: String,
    private val labelStop: String,
) {
    /** True while [AudioCaptureService] is actively recording. */
    var isRecording: Boolean = false
        private set

    /** The label the button should display in the current state. */
    val buttonLabel: String
        get() = if (isRecording) labelStop else labelRecord

    /** The button should always be enabled — it acts as a start/stop toggle. */
    val isButtonEnabled: Boolean
        get() = true

    /**
     * Call when the service has successfully started recording.
     * Returns false (no-op) if already recording.
     */
    fun onRecordingStarted(): Boolean {
        if (isRecording) return false
        isRecording = true
        return true
    }

    /**
     * Call when the service has stopped recording (success, error, or manual stop).
     * Returns false (no-op) if not currently recording.
     */
    fun onRecordingStopped(): Boolean {
        if (!isRecording) return false
        isRecording = false
        return true
    }

    /**
     * Call when the service fails to start *before* recording begins.
     * Resets state to idle.
     */
    fun onStartFailed() {
        isRecording = false
    }
}
