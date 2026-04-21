package com.uiblueprint.android

import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.uiblueprint.android.databinding.ActivityResourceBinding
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

/**
 * Resource screen for managing GitHub repositories and files available to AI.
 *
 * Features:
 * - Load GitHub repositories for a user
 * - Select repositories to include in AI context
 * - View and select files grouped by type
 * - Apply selections and return to chat
 */
class ResourceActivity : AppCompatActivity() {

    private lateinit var binding: ActivityResourceBinding
    private lateinit var prefs: SharedPreferences
    private var executor = Executors.newSingleThreadExecutor { Thread(it, "ResourceActivity-worker") }

    private lateinit var repoAdapter: GithubRepoAdapter
    private lateinit var fileAdapter: ChatFileAdapter

    private val chatFiles = mutableListOf<ChatFile>()
    // Track whether selections have been committed to the backend in this session.
    private var selectionsCommitted = false

    private var conversationId: String? = null

    private val filePickerLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent(),
    ) { uri: Uri? ->
        // Guard: never start an upload if the activity is already closing.
        if (!isFinishing && uri != null) {
            uploadFile(uri)
        }
    }

    companion object {
        private const val PREFS_NAME = "chat_prefs"
        private const val PREF_SELECTED_REPOS = "selected_repos"
        const val EXTRA_CONVERSATION_ID = "conversation_id"
        private val pollingExecutor = Executors.newCachedThreadPool { Thread(it, "IngestPolling-worker") }
        private val activePollingJobs = ConcurrentHashMap.newKeySet<String>()

        fun start(context: Context, conversationId: String?) {
            val intent = Intent(context, ResourceActivity::class.java)
            intent.putExtra(EXTRA_CONVERSATION_ID, conversationId)
            context.startActivity(intent)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityResourceBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        conversationId = intent.getStringExtra(EXTRA_CONVERSATION_ID)
        if (conversationId.isNullOrBlank()) {
            Toast.makeText(this, "No active conversation", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        // Pre-fill GitHub username if saved
        val savedUsername = prefs.getString("github_username", "")
        if (!savedUsername.isNullOrEmpty()) {
            binding.etGithubUsername.setText(savedUsername)
        }

        setupRepoList()
        setupFileList()
        initializeVisibility()

        // LAW 4 — EXIT ≠ APPLY: close is navigation — it must never trigger ingestion.
        binding.btnClose.setOnClickListener { finish() }

        binding.btnLoadRepos.setOnClickListener {
            val username = binding.etGithubUsername.text.toString().trim()
            if (username.isNotEmpty()) {
                // Save username for future use
                prefs.edit().putString("github_username", username).apply()
                loadGithubRepos(username)
            } else {
                Toast.makeText(this, "Enter a GitHub username", Toast.LENGTH_SHORT).show()
            }
        }

        binding.btnApply.setOnClickListener {
            applySelections()
        }

        binding.btnUploadFile.setOnClickListener {
            // Guard: never open the file picker while the activity is closing,
            // preventing an accidental upload if the close (X) tap's event
            // leaks into the upload button's touch area.
            if (isFinishing) return@setOnClickListener
            if (conversationId != null) {
                filePickerLauncher.launch("*/*")
            } else {
                Toast.makeText(this, "No active conversation", Toast.LENGTH_SHORT).show()
            }
        }

    }

    override fun onResume() {
        super.onResume()
        ensureExecutor()
        if (conversationId != null) {
            loadChatFiles()
            loadActiveRepos()
        }
    }

    private fun ensureExecutor() {
        if (executor.isShutdown || executor.isTerminated) {
            executor = Executors.newSingleThreadExecutor { Thread(it, "ResourceActivity-worker") }
        }
    }

    // LAW 4 — EXIT ≠ APPLY: back navigation must never trigger ingestion.
    // Selections are only applied when the user explicitly presses the Apply button.
    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        finish()
    }

    // PHASE 1 — PERSISTENT SELECTION STORE:
    // Persist the current selection state to SharedPreferences so it survives
    // back navigation, activity destruction, and process death.
    private fun persistSelections() {
        val array = JSONArray()
        for (repo in repoAdapter.selectedRepos()) {
            val obj = JSONObject().apply {
                put("repo_url", repo.htmlUrl)
                put("branch", repo.defaultBranch)
                put("full_name", repo.fullName)
            }
            array.put(obj)
        }
        prefs.edit().putString(PREF_SELECTED_REPOS, array.toString()).apply()
    }

    private fun selectedRepoUrls(): Set<String> {
        val raw = prefs.getString(PREF_SELECTED_REPOS, "[]") ?: "[]"
        return try {
            val arr = JSONArray(raw)
            (0 until arr.length()).map { arr.getJSONObject(it).getString("repo_url") }.toSet()
        } catch (e: Exception) {
            emptySet()
        }
    }

    // Restore saved selections onto the freshly loaded repo list.
    private fun restoreSelections(repos: List<GithubRepo>): List<GithubRepo> {
        val savedUrls = selectedRepoUrls()
        if (savedUrls.isEmpty()) return repos
        return repos.map { repo ->
            if (repo.htmlUrl in savedUrls) repo.copy(selected = true) else repo
        }
    }

    // PHASE 7 — AUTH CONSISTENCY: read API key from SharedPreferences so both
    // ResourceActivity and ChatActivity use the same credential source.
    // Falls back to BuildConfig if the prefs key is absent or empty.
    private fun apiKey(): String =
        prefs.getString("api_key", "").takeIf { !it.isNullOrEmpty() }
            ?: BuildConfig.BACKEND_API_KEY

    private fun baseUrl(): String =
        (prefs.getString("backend_url", "").takeIf { !it.isNullOrEmpty() }
            ?: BuildConfig.BACKEND_BASE_URL).trimEnd('/')

    private fun setupRepoList() {
        repoAdapter = GithubRepoAdapter(onSelectionChanged = { persistSelections() })
        binding.rvGithubRepos.layoutManager = LinearLayoutManager(this)
        binding.rvGithubRepos.adapter = repoAdapter
    }

    private fun setupFileList() {
        fileAdapter = ChatFileAdapter(object : ChatFileAdapter.FileActionListener {
            override fun onToggleIncludeInContext(file: ChatFile, included: Boolean) {
                // Update the file's context inclusion status
                file.includedInContext = included
            }

            override fun onRenameFile(file: ChatFile) {
                // Not used in resource view
            }

            override fun onDeleteFile(file: ChatFile) {
                // Not used in resource view
            }

            override fun onDownloadFile(file: ChatFile) {
                // Not used in resource view
            }
        })
        binding.rvFiles.layoutManager = LinearLayoutManager(this)
        binding.rvFiles.adapter = fileAdapter
    }

    private fun initializeVisibility() {
        // Initialize repository section - hide RecyclerView, show prompt text
        binding.rvGithubRepos.visibility = View.GONE
        binding.tvNoRepos.visibility = View.VISIBLE
        binding.tvNoRepos.text = getString(R.string.label_no_repos_loaded)
        
        // Initialize files section - if no conversation, show message
        if (conversationId == null) {
            binding.rvFiles.visibility = View.GONE
            binding.tvNoFiles.visibility = View.VISIBLE
            binding.tvNoFiles.text = "No active conversation"
            binding.btnUploadFile.isEnabled = false
        } else {
            binding.rvFiles.visibility = View.GONE
            binding.tvNoFiles.visibility = View.VISIBLE
            binding.tvNoFiles.text = getString(R.string.label_no_files_uploaded)
        }
    }

    private fun loadGithubRepos(username: String) {
        executor.execute {
            try {
                runOnUiThread {
                    binding.tvNoRepos.visibility = View.GONE
                    binding.btnLoadRepos.isEnabled = false
                    Toast.makeText(this, "Loading repositories…", Toast.LENGTH_SHORT).show()
                }

                val apiKey = apiKey()
                val baseUrl = baseUrl()

                val request = Request.Builder()
                    .url("$baseUrl/api/github/user/$username/repos?per_page=30")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .get()
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: "[]"
                    val reposArray = JSONArray(body)
                    val repos = mutableListOf<GithubRepo>()

                    for (i in 0 until reposArray.length()) {
                        val obj = reposArray.getJSONObject(i)
                        repos.add(
                            GithubRepo(
                                name = obj.getString("name"),
                                fullName = obj.getString("full_name"),
                                description = if (obj.isNull("description")) "" else obj.optString("description", ""),
                                htmlUrl = obj.getString("html_url"),
                                defaultBranch = obj.optString("default_branch", "main"),
                                language = if (obj.isNull("language")) "" else obj.optString("language", ""),
                                stars = obj.optInt("stargazers_count", 0),
                                isPrivate = obj.optBoolean("private", false),
                                selected = false
                            )
                        )
                    }

                    runOnUiThread {
                        val projectedRepos = restoreSelections(repos)
                        repoAdapter.submitList(projectedRepos)
                        binding.rvGithubRepos.visibility = if (repos.isEmpty()) View.GONE else View.VISIBLE
                        binding.tvNoRepos.visibility = if (repos.isEmpty()) View.VISIBLE else View.GONE
                        binding.btnLoadRepos.isEnabled = true
                        Toast.makeText(this, "Loaded ${repos.size} repositories", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    val errorMsg = when (response.code) {
                        404 -> "User '$username' not found"
                        401, 403 -> "Authentication failed. Check API key"
                        else -> "Failed to load repos (${response.code})"
                    }
                    runOnUiThread {
                        Toast.makeText(this, errorMsg, Toast.LENGTH_LONG).show()
                        binding.tvNoRepos.visibility = View.VISIBLE
                        binding.tvNoRepos.text = errorMsg
                        binding.rvGithubRepos.visibility = View.GONE
                        binding.btnLoadRepos.isEnabled = true
                    }
                    Log.e("ResourceActivity", "Failed to load repos: ${response.code} - ${response.message}")
                }
            } catch (e: SocketTimeoutException) {
                Log.e("ResourceActivity", "Timeout loading repos", e)
                runOnUiThread {
                    val errorMsg = getString(R.string.error_backend_timeout)
                    Toast.makeText(this, errorMsg, Toast.LENGTH_LONG).show()
                    binding.tvNoRepos.visibility = View.VISIBLE
                    binding.tvNoRepos.text = errorMsg
                    binding.rvGithubRepos.visibility = View.GONE
                    binding.btnLoadRepos.isEnabled = true
                }
            } catch (e: ConnectException) {
                Log.e("ResourceActivity", "Connection failed loading repos", e)
                runOnUiThread {
                    val errorMsg = getString(R.string.error_backend_connection_failed)
                    Toast.makeText(this, errorMsg, Toast.LENGTH_LONG).show()
                    binding.tvNoRepos.visibility = View.VISIBLE
                    binding.tvNoRepos.text = errorMsg
                    binding.rvGithubRepos.visibility = View.GONE
                    binding.btnLoadRepos.isEnabled = true
                }
            } catch (e: IOException) {
                Log.e("ResourceActivity", "Network error loading repos", e)
                runOnUiThread {
                    val errorMsg = getString(R.string.error_network_error, e.message ?: "Unknown")
                    Toast.makeText(this, errorMsg, Toast.LENGTH_LONG).show()
                    binding.tvNoRepos.visibility = View.VISIBLE
                    binding.tvNoRepos.text = errorMsg
                    binding.rvGithubRepos.visibility = View.GONE
                    binding.btnLoadRepos.isEnabled = true
                }
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error loading repos", e)
                runOnUiThread {
                    val errorMsg = getString(R.string.error_unknown, e.message ?: "Unknown")
                    Toast.makeText(this, errorMsg, Toast.LENGTH_LONG).show()
                    binding.tvNoRepos.visibility = View.VISIBLE
                    binding.tvNoRepos.text = errorMsg
                    binding.rvGithubRepos.visibility = View.GONE
                    binding.btnLoadRepos.isEnabled = true
                }
            }
        }
    }

    private fun loadChatFiles() {
        val convId = conversationId ?: return
        executor.execute {
            try {
                val apiKey = apiKey()
                val baseUrl = baseUrl()

                val request = Request.Builder()
                    .url("$baseUrl/api/chat/$convId/files")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .get()
                    .build()

                val response = BackendClient.executeWithRetry(request)
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: "[]"
                    val filesArray = JSONArray(body)
                    val files = mutableListOf<ChatFile>()

                    for (i in 0 until filesArray.length()) {
                        val obj = filesArray.getJSONObject(i)
                        val dateFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US)
                        files.add(
                            ChatFile(
                                id = obj.getString("id"),
                                conversationId = obj.getString("conversation_id"),
                                filename = obj.getString("filename"),
                                mimeType = obj.getString("mime_type"),
                                sizeBytes = obj.getLong("size_bytes"),
                                category = obj.getString("category"),
                                includedInContext = obj.getBoolean("included_in_context"),
                                createdAt = dateFormat.parse(obj.getString("created_at").split(".")[0]) ?: Date(),
                                updatedAt = dateFormat.parse(obj.getString("updated_at").split(".")[0]) ?: Date(),
                                downloadUrl = obj.optString("download_url", null),
                                // Files returned from the server are fully ingested
                                ingestStatus = IngestStatus.AVAILABLE,
                            )
                        )
                    }

                    runOnUiThread {
                        chatFiles.clear()
                        chatFiles.addAll(files)
                        fileAdapter.submitList(chatFiles)
                        binding.rvFiles.visibility = if (files.isEmpty()) View.GONE else View.VISIBLE
                        binding.tvNoFiles.visibility = if (files.isEmpty()) View.VISIBLE else View.GONE
                    }
                } else {
                    Log.e("ResourceActivity", "Failed to load files: ${response.code}")
                    runOnUiThread {
                        binding.rvFiles.visibility = View.GONE
                        binding.tvNoFiles.visibility = View.VISIBLE
                        binding.tvNoFiles.text = "Failed to load files (${response.code})"
                    }
                }
            } catch (e: SocketTimeoutException) {
                Log.e("ResourceActivity", "Timeout loading files", e)
                runOnUiThread {
                    binding.rvFiles.visibility = View.GONE
                    binding.tvNoFiles.visibility = View.VISIBLE
                    binding.tvNoFiles.text = getString(R.string.error_backend_timeout)
                }
            } catch (e: ConnectException) {
                Log.e("ResourceActivity", "Connection failed loading files", e)
                runOnUiThread {
                    binding.rvFiles.visibility = View.GONE
                    binding.tvNoFiles.visibility = View.VISIBLE
                    binding.tvNoFiles.text = getString(R.string.error_backend_connection_failed)
                }
            } catch (e: IOException) {
                Log.e("ResourceActivity", "Network error loading files", e)
                runOnUiThread {
                    binding.rvFiles.visibility = View.GONE
                    binding.tvNoFiles.visibility = View.VISIBLE
                    binding.tvNoFiles.text = getString(R.string.error_network_error, e.message ?: "Unknown")
                }
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error loading files", e)
                runOnUiThread {
                    binding.rvFiles.visibility = View.GONE
                    binding.tvNoFiles.visibility = View.VISIBLE
                    binding.tvNoFiles.text = getString(R.string.error_unknown, e.message ?: "Unknown")
                }
            }
        }
    }

    private fun uploadFile(uri: Uri) {
        val convId = conversationId ?: run {
            Toast.makeText(this, "No active conversation", Toast.LENGTH_SHORT).show()
            return
        }

        executor.execute {
            try {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.status_uploading_file), Toast.LENGTH_SHORT).show()
                }

                val apiKey = apiKey()
                val baseUrl = baseUrl()

                // MQP-CONTRACT: INGESTION_UI_STATE_ENFORCEMENT_V3 — Receive job_id
                val jobId = ChatFileUploadHelper.uploadFile(
                    uri = uri,
                    conversationId = convId,
                    apiKey = apiKey,
                    baseUrl = baseUrl,
                    contentResolver = contentResolver,
                    cacheDir = cacheDir,
                    onProgress = null
                )

                if (jobId != null) {
                    runOnUiThread {
                        // STEP 4: EVENT FEEDBACK — After POST
                        Toast.makeText(this, "File upload initiated — processing…", Toast.LENGTH_SHORT).show()
                    }
                    
                    startPolling(jobId)
                } else {
                    runOnUiThread {
                        Toast.makeText(this, "Failed to upload file", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: SocketTimeoutException) {
                Log.e("ResourceActivity", "Timeout uploading file", e)
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.error_backend_timeout), Toast.LENGTH_LONG).show()
                }
            } catch (e: ConnectException) {
                Log.e("ResourceActivity", "Connection failed uploading file", e)
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.error_backend_connection_failed), Toast.LENGTH_LONG).show()
                }
            } catch (e: IOException) {
                Log.e("ResourceActivity", "Network error uploading file", e)
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.error_file_upload_network, e.message ?: "Unknown"), Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error uploading file", e)
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.error_file_upload_generic, e.message ?: "Unknown"), Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    private fun applySelections() {
        val convId = conversationId ?: run {
            finish()
            return
        }

        val selectedRepos = repoAdapter.selectedRepos()
        val modifiedFiles = chatFiles.filter { file ->
            // Check if the file's inclusion state has been modified
            // For now, we'll update all files since we track their state
            true
        }

        executor.execute {
            try {
                runOnUiThread {
                    binding.btnApply.isEnabled = false
                    Toast.makeText(this, "Applying selections…", Toast.LENGTH_SHORT).show()
                }

                val apiKey = apiKey()
                val baseUrl = baseUrl()

                var successCount = 0
                var failureCount = 0

                // Add selected GitHub repos via the new unified ingestion endpoint.
                for (repo in selectedRepos) {
                    try {
                        // Use POST /v1/ingest/repo (unified IngestJob pipeline)
                        val jsonBody = JSONObject().apply {
                            put("repo_url", repo.htmlUrl)
                            put("branch", repo.defaultBranch)
                            put("conversation_id", convId)
                            put("force_refresh", false)
                        }.toString()

                        val request = Request.Builder()
                            .url("$baseUrl/v1/ingest/repo")
                            .addHeader("Authorization", "Bearer $apiKey")
                            .addHeader("Content-Type", "application/json")
                            .post(jsonBody.toRequestBody("application/json".toMediaType()))
                            .build()

                        val response = BackendClient.executeWithRetry(request)
                        when (response.code) {
                            202 -> {
                                // Success - job queued, extract job_id from response
                                val responseBody = response.body?.string() ?: "{}"
                                val responseJson = JSONObject(responseBody)
                                val jobId = responseJson.getString("job_id")
                                successCount++
                                Log.d("ResourceActivity", "Repo ${repo.fullName} queued with job_id=$jobId")
                            }
                            else -> {
                                failureCount++
                                Log.e("ResourceActivity", "Failed to add repo ${repo.fullName}: ${response.code}")
                            }
                        }
                    } catch (e: Exception) {
                        failureCount++
                        Log.e("ResourceActivity", "Error adding repo ${repo.fullName}", e)
                    }
                }

                // Update file context inclusion
                for (file in modifiedFiles) {
                    try {
                        val jsonBody = JSONObject().apply {
                            put("included_in_context", file.includedInContext)
                        }.toString()

                        val request = Request.Builder()
                            .url("$baseUrl/api/chat/$convId/files/${file.id}")
                            .addHeader("Authorization", "Bearer $apiKey")
                            .addHeader("Content-Type", "application/json")
                            .patch(jsonBody.toRequestBody("application/json".toMediaType()))
                            .build()

                        val response = BackendClient.executeWithRetry(request)
                        if (!response.isSuccessful) {
                            Log.e("ResourceActivity", "Failed to update file ${file.filename}: ${response.code}")
                        }
                    } catch (e: Exception) {
                        Log.e("ResourceActivity", "Error updating file ${file.filename}", e)
                    }
                }

                runOnUiThread {
                    binding.btnApply.isEnabled = true
                    if (failureCount == 0) {
                        // Clear persisted selections — they are now committed to backend
                        prefs.edit().remove(PREF_SELECTED_REPOS).apply()
                        selectionsCommitted = true
                        Toast.makeText(this, "Selections applied successfully", Toast.LENGTH_SHORT).show()
                        
                        loadActiveRepos()
                        finish()
                    } else {
                        Toast.makeText(
                            this,
                            "Applied: $successCount | Failed: $failureCount",
                            Toast.LENGTH_LONG
                        ).show()
                    }
                }
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error applying selections", e)
                runOnUiThread {
                    binding.btnApply.isEnabled = true
                    Toast.makeText(this, "Failed to apply selections: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    private fun startPolling(jobId: String) {
        if (!activePollingJobs.add(jobId)) return
        val apiKey = apiKey()
        val baseUrl = baseUrl()
        pollingExecutor.execute {
            try {
                while (true) {
                    try {
                        val request = Request.Builder()
                            .url("$baseUrl/jobs/$jobId")
                            .addHeader("Authorization", "Bearer $apiKey")
                            .get()
                            .build()

                        val response = BackendClient.executeWithRetry(request)
                        if (!response.isSuccessful) {
                            response.close()
                            Thread.sleep(2000)
                            continue
                        }
                        val body = response.body?.string()
                        response.close()
                        if (body != null) {
                            val json = JSONObject(body)
                            runOnUiThread { renderPolledJob(json) }
                            val status = json.optString("status")
                            if (status == "success" || status == "failed") {
                                break
                            }
                        }
                        Thread.sleep(2000)
                    } catch (_: Exception) {
                        Log.w("ResourceActivity", "Polling fetch failed for job_id=$jobId; retrying")
                        Thread.sleep(2000)
                    }
                }
            } finally {
                activePollingJobs.remove(jobId)
            }
        }
    }

    private fun renderPolledJob(jobJson: JSONObject) {
        if (jobJson.optString("kind") == "repo") {
            loadActiveRepos()
        } else {
            val status = jobJson.optString("status")
            if (status == "failed") {
                val error = jobJson.optString("error")
                Toast.makeText(
                    this,
                    "File ingestion failed: $error",
                    Toast.LENGTH_LONG
                ).show()
            }
            if (status == "success") {
                loadChatFiles()
            }
        }
    }

    /**
     * Load active repos for this conversation from backend truth surfaces.
     * Called after applySelections and on screen entry/resume.
     * SOURCE OF TRUTH:
     * - GET /api/chat/{conversation_id}/repos
     * - GET /repos/{repo_id}/structure (debug visibility surface)
     */
    private fun loadActiveRepos() {
        val convId = conversationId ?: return
        executor.execute {
            try {
                val apiKey = apiKey()
                val baseUrl = baseUrl()
                val request = Request.Builder()
                    .url("$baseUrl/api/chat/$convId/repos")
                    .addHeader("Authorization", "Bearer $apiKey")
                    .get()
                    .build()
                val response = BackendClient.executeWithRetry(request)
                if (!response.isSuccessful) return@execute

                val body = response.body?.string() ?: "[]"
                val arr = JSONArray(body)
                val repos = mutableListOf<GithubRepo>()

                for (i in 0 until arr.length()) {
                    val obj = arr.getJSONObject(i)
                    val repoId = obj.getString("id")
                    val fallbackFiles = obj.optInt("total_files", 0)
                    val fallbackChunks = obj.optInt("chunk_count", 0)
                    val fallbackIndexed = obj.optString("status") == "success"
                    var files = fallbackFiles
                    var chunks = fallbackChunks
                    var indexed = fallbackIndexed
                    var lastRetrieved = 0

                    try {
                        val structureReq = Request.Builder()
                            .url("$baseUrl/repos/$repoId/structure")
                            .addHeader("Authorization", "Bearer $apiKey")
                            .get()
                            .build()
                        val structureResp = BackendClient.executeWithRetry(structureReq)
                        if (structureResp.isSuccessful) {
                            val structureJson = JSONObject(structureResp.body?.string() ?: "{}")
                            files = structureJson.optInt("total_files", files)
                            chunks = structureJson.optInt("total_chunks", chunks)
                            indexed = structureJson.optBoolean("indexed", indexed)
                            lastRetrieved = structureJson.optInt("last_retrieved_count", 0)
                        }
                    } catch (e: Exception) {
                        Log.w("ResourceActivity", "Structure fetch failed for repo_id=$repoId: ${e.message}")
                    }

                    val repoUrl = obj.optString("repo_url")
                    val owner = obj.optString("owner")
                    val name = obj.optString("name")
                    repos.add(
                        GithubRepo(
                            name = name.ifEmpty { repoUrl.substringAfterLast("/") },
                            fullName = if (owner.isNotEmpty() && name.isNotEmpty()) "$owner/$name" else repoUrl.removePrefix("https://github.com/"),
                            description = "",
                            htmlUrl = repoUrl,
                            defaultBranch = obj.optString("branch", "main"),
                            language = "",
                            stars = 0,
                            isPrivate = false,
                            selected = false,
                            ingestionStatus = obj.optString("status"),
                            progress = 0,
                            totalFiles = files,
                            totalChunks = chunks,
                            indexed = indexed,
                            lastRetrievedCount = lastRetrieved,
                            errorMessage = null,
                            ingestJobId = null
                        )
                    )
                }

                runOnUiThread {
                    val projectedRepos = restoreSelections(repos)
                    repoAdapter.submitList(projectedRepos)
                    binding.rvGithubRepos.visibility = if (projectedRepos.isEmpty()) View.GONE else View.VISIBLE
                    binding.tvNoRepos.visibility = if (projectedRepos.isEmpty()) View.VISIBLE else View.GONE
                }
            } catch (e: Exception) {
                Log.w("ResourceActivity", "loadActiveRepos failed: ${e.message}")
            }
        }
    }
}
