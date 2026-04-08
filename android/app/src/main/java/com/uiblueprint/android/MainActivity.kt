package com.uiblueprint.android

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.Build
import android.os.Bundle
import android.os.Looper
import android.os.SystemClock
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import com.uiblueprint.android.databinding.ActivityMainBinding
import java.io.File
import java.util.UUID

/**
 * Main screen.
 *
 * Shows a "Record 20 s" button.  When tapped:
 * 1. Requests MediaProjection permission.
 * 2. Optionally requests RECORD_AUDIO (API >= 29) for internal audio capture.
 * 3. Starts CaptureService (foreground, mediaProjection type).
 * 4. CaptureService records 20 s and broadcasts CAPTURE_DONE.
 * 5. MainActivity inserts the clip into the device Gallery via MediaStore.
 * 6. A RecyclerView session list shows [saved] or [failed] status; tapping a saved item plays it.
 *
 * Backend upload is disabled by default; see [MediaStoreVideoSaver] for local storage.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var captureResultStore: CaptureResultStore
    private val watchdogHandler = Handler(Looper.getMainLooper())
    private val recordingCompletionHelper = RecordingCompletionHelper(RECORDING_TIMEOUT_MS)
    private val sessions = mutableListOf<SessionItem>()
    private lateinit var sessionAdapter: SessionAdapter
    private val recordingWatchdogRunnable = Runnable {
        val startedAtMs = captureResultStore.getRecordingStartedAtMs() ?: return@Runnable
        if (recordingCompletionHelper.hasTimedOut(startedAtMs, SystemClock.elapsedRealtime())) {
            handleRecordingTimeout()
        }
    }

    // Whether RECORD_AUDIO was granted when the most recent record attempt began.
    private var audioPermissionGranted = false

    // MediaProjection permission launcher.
    private val projectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            startCapture(result.resultCode, result.data!!)
        } else {
            handlePermissionDenied()
        }
    }

    // Notification permission launcher (Android 13+).
    private val notificationLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) {
        // Permission result handled silently; foreground service notification will still show
        // on older Android versions even without the permission.
        requestAudioThenScreenCapture()
    }

    // RECORD_AUDIO permission launcher — required for internal audio capture (API >= 29).
    private val audioPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        audioPermissionGranted = granted
        if (!granted) {
            Toast.makeText(this, getString(R.string.msg_audio_permission_denied), Toast.LENGTH_LONG).show()
        }
        requestScreenCapture()
    }

    // Receives CAPTURE_DONE broadcast from CaptureService.
    private val captureReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            processCaptureDone(
                CaptureDoneEvent(
                    clipPath = intent.getStringExtra(CaptureService.EXTRA_CLIP_PATH),
                    error = intent.getStringExtra(CaptureService.EXTRA_ERROR),
                    recordingDurationMs = intent.takeIf {
                        it.hasExtra(CaptureService.EXTRA_RECORDING_DURATION_MS)
                    }?.getIntExtra(CaptureService.EXTRA_RECORDING_DURATION_MS, 0),
                    schemaVersion = intent.getStringExtra(CaptureService.EXTRA_SCHEMA_VERSION)
                        ?: CaptureDoneEvent.SCHEMA_VERSION,
                ),
            )
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        captureResultStore = SharedPreferencesCaptureResultStore(applicationContext)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        sessionAdapter = SessionAdapter(sessions) { item -> openVideoPlayer(item) }
        binding.rvSessions.layoutManager = LinearLayoutManager(this)
        binding.rvSessions.adapter = sessionAdapter

        binding.btnRecord.setOnClickListener { onRecordClicked() }
        showIdleUi()
    }

    override fun onResume() {
        super.onResume()
        ContextCompat.registerReceiver(
            this,
            captureReceiver,
            IntentFilter(CaptureService.ACTION_CAPTURE_DONE),
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        recoverPendingCaptureState()
    }

    override fun onPause() {
        super.onPause()
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        unregisterReceiver(captureReceiver)
    }

    // -------------------------------------------------------------------------
    // Recording flow
    // -------------------------------------------------------------------------

    private fun onRecordClicked() {
        showRequestingPermissionUi()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notificationLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        } else {
            requestAudioThenScreenCapture()
        }
    }

    /**
     * On API >= 29, requests RECORD_AUDIO if not already granted (required for internal audio
     * capture).  On older APIs shows a toast explaining the limitation, then goes straight to
     * screen-capture consent.
     */
    private fun requestAudioThenScreenCapture() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            // Internal audio capture is not available; proceed video-only.
            audioPermissionGranted = false
            Toast.makeText(this, getString(R.string.msg_audio_requires_api29), Toast.LENGTH_LONG).show()
            requestScreenCapture()
            return
        }

        val alreadyGranted = ContextCompat.checkSelfPermission(
            this, android.Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED

        if (alreadyGranted) {
            audioPermissionGranted = true
            requestScreenCapture()
        } else {
            audioPermissionLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun requestScreenCapture() {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        projectionLauncher.launch(mpm.createScreenCaptureIntent())
    }

    private fun startCapture(resultCode: Int, data: Intent) {
        captureResultStore.clearLastResult()
        captureResultStore.markRecordingStarted(SystemClock.elapsedRealtime())
        showRecordingUi(audioPermissionGranted)
        scheduleRecordingWatchdog(RECORDING_TIMEOUT_MS)
        val intent = Intent(this, CaptureService::class.java).apply {
            putExtra(CaptureService.EXTRA_RESULT_CODE, resultCode)
            putExtra(CaptureService.EXTRA_RESULT_DATA, data)
            putExtra(CaptureService.EXTRA_AUDIO_ENABLED, audioPermissionGranted)
        }
        try {
            startForegroundService(intent)
        } catch (_: Exception) {
            watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
            clearRecoveryState()
            showIdleUi()
            showCaptureError(ERROR_START_FAILED)
        }
    }

    private fun onCaptureDone(clip: File) {
        val sessionId = UUID.randomUUID().toString()
        when (val result = MediaStoreVideoSaver.saveClipToGallery(applicationContext, clip)) {
            is MediaStoreVideoSaver.SaveResult.Success -> {
                sessions.add(0, SessionItem(sessionId, STATUS_SAVED, result.displayName, result.uriString))
                sessionAdapter.notifyItemInserted(0)
                Toast.makeText(this, "Saved to Gallery", Toast.LENGTH_SHORT).show()
            }
            is MediaStoreVideoSaver.SaveResult.Failure -> {
                sessions.add(0, SessionItem(sessionId, STATUS_FAILED, clip.name))
                sessionAdapter.notifyItemInserted(0)
                showCaptureError(result.userMessage)
            }
        }
    }

    private fun processCaptureDone(event: CaptureDoneEvent) {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        showIdleUi()
        try {
            val normalizedEvent = recordingCompletionHelper.normalize(event)
            val error = normalizedEvent.error
            if (error != null) {
                showCaptureError(error)
                return
            }

            val clipPath = normalizedEvent.clipPath
            if (clipPath == null) {
                showCaptureError(CaptureDoneEvent.ERROR_NO_OUTPUT)
                return
            }
            onCaptureDone(File(clipPath))
        } finally {
            clearRecoveryState()
        }
    }

    private fun handleRecordingTimeout() {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        clearRecoveryState()
        showIdleUi()
        showCaptureError(CaptureDoneEvent.ERROR_TIMEOUT)
    }

    private fun handlePermissionDenied() {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        clearRecoveryState()
        showIdleUi()
        Toast.makeText(this, ERROR_PERMISSION_DENIED, Toast.LENGTH_SHORT).show()
    }

    private fun recoverPendingCaptureState() {
        captureResultStore.getLastResult()?.let {
            processCaptureDone(it)
            return
        }

        val startedAtMs = captureResultStore.getRecordingStartedAtMs() ?: run {
            showIdleUi()
            return
        }
        val nowMs = SystemClock.elapsedRealtime()
        if (recordingCompletionHelper.hasTimedOut(startedAtMs, nowMs)) {
            handleRecordingTimeout()
            return
        }

        showRecordingUi()
        scheduleRecordingWatchdog(recordingCompletionHelper.remainingTimeoutMs(startedAtMs, nowMs))
    }

    private fun clearRecoveryState() {
        captureResultStore.clearLastResult()
        captureResultStore.clearRecordingStarted()
    }

    private fun scheduleRecordingWatchdog(delayMs: Long) {
        watchdogHandler.removeCallbacks(recordingWatchdogRunnable)
        watchdogHandler.postDelayed(recordingWatchdogRunnable, delayMs)
    }

    private fun showIdleUi() {
        binding.btnRecord.isEnabled = true
        binding.tvStatus.text = getString(R.string.status_idle)
    }

    private fun showRequestingPermissionUi() {
        binding.btnRecord.isEnabled = false
        binding.tvStatus.text = getString(R.string.status_requesting_permission)
    }

    private fun showRecordingUi(withAudio: Boolean = false) {
        binding.btnRecord.isEnabled = false
        binding.tvStatus.text = if (withAudio) {
            getString(R.string.status_recording_with_audio)
        } else {
            getString(R.string.status_recording)
        }
    }

    private fun showCaptureError(message: String) {
        val displayMessage = if (message.startsWith(ERROR_PREFIX, ignoreCase = true)) {
            message
        } else {
            "$ERROR_PREFIX: $message"
        }
        Toast.makeText(this, displayMessage, Toast.LENGTH_LONG).show()
    }

    // -------------------------------------------------------------------------
    // Session list interaction
    // -------------------------------------------------------------------------

    private fun openVideoPlayer(item: SessionItem) {
        val uriString = item.uri
        if (uriString.isNullOrBlank()) {
            Toast.makeText(this, getString(R.string.error_video_open), Toast.LENGTH_SHORT).show()
            return
        }
        val intent = Intent(this, VideoPlayerActivity::class.java).apply {
            putExtra(VideoPlayerActivity.EXTRA_URI_STRING, uriString)
        }
        startActivity(intent)
    }

    data class SessionItem(val id: String, val status: String, val label: String, val uri: String? = null)

    companion object {
        private const val RECORDING_TIMEOUT_MS = 30_000L
        private const val ERROR_PREFIX = "Capture failed"
        private const val ERROR_PERMISSION_DENIED = "Screen capture permission denied"
        private const val ERROR_START_FAILED = "Capture failed to start recording."
        const val STATUS_SAVED = "saved"
        const val STATUS_FAILED = "failed"
    }
}
