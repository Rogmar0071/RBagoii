package com.uiblueprint.android

import android.net.Uri
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.uiblueprint.android.databinding.ActivityAnalysisBinding
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.Response
import okio.BufferedSink
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Analysis screen: prompts the user for requirements text, uploads the saved clip to
 * [BuildConfig.ANALYSIS_BASE_URL]/v1/analyze, and displays the structured result.
 *
 * Expects:
 *  [EXTRA_VIDEO_URI]   – MediaStore content URI string of the saved clip.
 *  [EXTRA_VIDEO_LABEL] – (optional) human-readable filename shown in the toolbar.
 */
class AnalysisActivity : AppCompatActivity() {

    private lateinit var binding: ActivityAnalysisBinding

    private val httpClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .build()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityAnalysisBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val videoUriString = intent.getStringExtra(EXTRA_VIDEO_URI)
        if (videoUriString.isNullOrBlank()) {
            Toast.makeText(this, getString(R.string.error_video_open), Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        val label = intent.getStringExtra(EXTRA_VIDEO_LABEL) ?: getString(R.string.label_analysis)
        supportActionBar?.title = label
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        binding.btnAnalyze.setOnClickListener {
            val requirements = binding.etRequirements.text?.toString()?.trim() ?: ""
            if (requirements.isEmpty()) {
                binding.etRequirements.error = getString(R.string.error_requirements_empty)
                return@setOnClickListener
            }
            startAnalysis(videoUriString, requirements)
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    private fun startAnalysis(videoUriString: String, requirements: String) {
        showLoading(true)
        binding.tvResults.visibility = View.GONE

        val videoUri = try {
            Uri.parse(videoUriString)
        } catch (_: Exception) {
            showError(getString(R.string.error_video_open))
            showLoading(false)
            return
        }

        val videoMediaType = "video/mp4".toMediaTypeOrNull()
        val videoBody = object : RequestBody() {
            override fun contentType() = videoMediaType
            override fun writeTo(sink: BufferedSink) {
                contentResolver.openInputStream(videoUri)?.use { input ->
                    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                    var read: Int
                    while (input.read(buffer).also { read = it } != -1) {
                        sink.write(buffer, 0, read)
                    }
                } ?: throw IOException(getString(R.string.error_video_open))
            }
        }

        val requestBody = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("video", "recording.mp4", videoBody)
            .addFormDataPart("requirements", requirements)
            .build()

        val baseUrl = BuildConfig.ANALYSIS_BASE_URL.trimEnd('/')
        val request = Request.Builder()
            .url("$baseUrl/v1/analyze")
            .post(requestBody)
            .build()

        httpClient.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnUiThread {
                    showLoading(false)
                    showError(getString(R.string.error_analysis_network, e.message ?: ""))
                }
            }

            override fun onResponse(call: Call, response: Response) {
                val body = response.body?.string() ?: ""
                runOnUiThread {
                    showLoading(false)
                    if (response.isSuccessful) {
                        showResults(body)
                    } else {
                        val detail = extractErrorDetail(body, response.code)
                        showError(getString(R.string.error_analysis_server, response.code, detail))
                    }
                }
            }
        })
    }

    private fun showLoading(loading: Boolean) {
        binding.progressBar.visibility = if (loading) View.VISIBLE else View.GONE
        binding.btnAnalyze.isEnabled = !loading
        binding.etRequirements.isEnabled = !loading
    }

    private fun showResults(jsonBody: String) {
        val sb = StringBuilder()
        try {
            val obj = JSONObject(jsonBody)

            sb.append(getString(R.string.label_summary)).append("\n")
            sb.append(obj.optString("summary", "—")).append("\n\n")

            sb.append(getString(R.string.label_conclusions)).append("\n")
            val conclusions = obj.optJSONArray("conclusions")
            if (conclusions != null && conclusions.length() > 0) {
                for (i in 0 until conclusions.length()) {
                    sb.append("• ").append(conclusions.getString(i)).append("\n")
                }
            } else {
                sb.append("—\n")
            }
            sb.append("\n")

            sb.append(getString(R.string.label_key_events)).append("\n")
            val events = obj.optJSONArray("key_events")
            if (events != null && events.length() > 0) {
                for (i in 0 until events.length()) {
                    val ev = events.getJSONObject(i)
                    val tSec = ev.optDouble("t_sec", 0.0)
                    val event = ev.optString("event", "")
                    sb.append(String.format("  %.1fs – %s\n", tSec, event))
                }
            } else {
                sb.append("—\n")
            }
            sb.append("\n")

            val confidence = obj.optDouble("confidence", -1.0)
            if (confidence >= 0) {
                sb.append(getString(R.string.label_confidence))
                sb.append(String.format(" %.0f%%\n\n", confidence * 100))
            }

            val diag = obj.optJSONObject("diagnostics")
            if (diag != null) {
                sb.append(getString(R.string.label_diagnostics)).append("\n")
                sb.append("  frames_used: ").append(diag.optInt("frames_used", 0)).append("\n")
                sb.append("  audio_present: ").append(diag.optBoolean("audio_present")).append("\n")
                sb.append("  transcript_used: ").append(diag.optBoolean("transcript_used")).append("\n")
            }
        } catch (_: JSONException) {
            sb.append(jsonBody)
        }

        binding.tvResults.text = sb.toString()
        binding.tvResults.visibility = View.VISIBLE
        binding.scrollResults.visibility = View.VISIBLE
    }

    private fun showError(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    private fun extractErrorDetail(body: String, code: Int): String {
        return try {
            JSONObject(body).optString("detail", "HTTP $code")
        } catch (_: JSONException) {
            "HTTP $code"
        }
    }

    companion object {
        const val EXTRA_VIDEO_URI = "analysis_video_uri"
        const val EXTRA_VIDEO_LABEL = "analysis_video_label"
    }
}
