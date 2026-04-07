package com.uiblueprint.android

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.work.WorkInfo
import androidx.work.WorkManager
import com.uiblueprint.android.databinding.ActivityMainBinding
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Main screen.
 *
 * Shows a "Record 10 s" button.  When tapped:
 * 1. Requests MediaProjection permission.
 * 2. Starts CaptureService (foreground, mediaProjection type).
 * 3. CaptureService records 10 s and broadcasts CAPTURE_DONE.
 * 4. MainActivity picks up the broadcast and enqueues UploadWorker.
 * 5. A simple session list (in-memory) shows status of each upload.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val sessions = mutableListOf<SessionItem>()

    // MediaProjection permission launcher.
    private val projectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            startCapture(result.resultCode, result.data!!)
        } else {
            Toast.makeText(this, "Screen capture permission denied", Toast.LENGTH_SHORT).show()
            resetUi()
        }
    }

    // Notification permission launcher (Android 13+).
    private val notificationLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) {
        // Permission result handled silently; foreground service notification will still show
        // on older Android versions even without the permission.
        requestScreenCapture()
    }

    // Receives CAPTURE_DONE broadcast from CaptureService.
    private val captureReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val error = intent.getStringExtra(CaptureService.EXTRA_ERROR)
            if (error != null) {
                Toast.makeText(this@MainActivity, "Capture failed: $error", Toast.LENGTH_LONG).show()
                resetUi()
                return
            }
            val clipPath = intent.getStringExtra(CaptureService.EXTRA_CLIP_PATH) ?: return
            onCaptureDone(File(clipPath))
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnRecord.setOnClickListener { onRecordClicked() }
        renderSessionList()
    }

    override fun onResume() {
        super.onResume()
        ContextCompat.registerReceiver(
            this,
            captureReceiver,
            IntentFilter(CaptureService.ACTION_CAPTURE_DONE),
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
    }

    override fun onPause() {
        super.onPause()
        unregisterReceiver(captureReceiver)
    }

    // -------------------------------------------------------------------------
    // Recording flow
    // -------------------------------------------------------------------------

    private fun onRecordClicked() {
        binding.btnRecord.isEnabled = false
        binding.tvStatus.text = getString(R.string.status_requesting_permission)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notificationLauncher.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        } else {
            requestScreenCapture()
        }
    }

    private fun requestScreenCapture() {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        projectionLauncher.launch(mpm.createScreenCaptureIntent())
    }

    private fun startCapture(resultCode: Int, data: Intent) {
        binding.tvStatus.text = getString(R.string.status_recording)
        val intent = Intent(this, CaptureService::class.java).apply {
            putExtra(CaptureService.EXTRA_RESULT_CODE, resultCode)
            putExtra(CaptureService.EXTRA_RESULT_DATA, data)
        }
        startForegroundService(intent)
    }

    private fun onCaptureDone(clip: File) {
        binding.tvStatus.text = getString(R.string.status_uploading)
        val meta = buildMeta()
        val sessionId = UploadWorker.enqueue(applicationContext, clip.absolutePath, meta)
        sessions.add(0, SessionItem(sessionId, STATUS_ENQUEUED, clip.name))
        renderSessionList()
        observeWorkerStatus(sessionId)
        resetUi()
    }

    private fun buildMeta(): String {
        return JSONObject().apply {
            put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
            put("os_version", Build.VERSION.RELEASE)
            put("sdk_int", Build.VERSION.SDK_INT)
            put("timestamp", SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).format(Date()))
        }.toString()
    }

    private fun observeWorkerStatus(sessionId: String) {
        // Each call observes a distinct LiveData keyed by the unique sessionId tag.
        // All observers are automatically removed when the Activity is destroyed.
        WorkManager.getInstance(this)
            .getWorkInfosByTagLiveData(sessionId)
            .observe(this) { workInfos ->
                val info = workInfos?.firstOrNull() ?: return@observe
                val status = when (info.state) {
                    WorkInfo.State.ENQUEUED -> STATUS_ENQUEUED
                    WorkInfo.State.RUNNING -> STATUS_UPLOADING
                    WorkInfo.State.SUCCEEDED -> STATUS_COMPLETED
                    WorkInfo.State.FAILED -> STATUS_FAILED
                    WorkInfo.State.BLOCKED -> STATUS_BLOCKED
                    WorkInfo.State.CANCELLED -> STATUS_CANCELLED
                }
                val idx = sessions.indexOfFirst { it.id == sessionId }
                if (idx >= 0) {
                    sessions[idx] = sessions[idx].copy(status = status)
                    renderSessionList()
                }
            }
    }

    private fun resetUi() {
        binding.btnRecord.isEnabled = true
        binding.tvStatus.text = getString(R.string.status_idle)
    }

    // -------------------------------------------------------------------------
    // Session list rendering
    // -------------------------------------------------------------------------

    private fun renderSessionList() {
        if (sessions.isEmpty()) {
            binding.tvSessions.visibility = View.GONE
            return
        }
        binding.tvSessions.visibility = View.VISIBLE
        binding.tvSessions.text = sessions.joinToString("\n") { "• ${it.label}  [${it.status}]" }
    }

    data class SessionItem(val id: String, val status: String, val label: String)

    companion object {
        const val STATUS_ENQUEUED = "enqueued"
        const val STATUS_UPLOADING = "uploading"
        const val STATUS_COMPLETED = "completed"
        const val STATUS_FAILED = "failed"
        const val STATUS_BLOCKED = "blocked"
        const val STATUS_CANCELLED = "cancelled"
    }
}
