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
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
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
    private val executor = Executors.newSingleThreadExecutor { Thread(it, "ResourceActivity-worker") }

    private lateinit var repoAdapter: GithubRepoAdapter
    private lateinit var fileAdapter: ChatFileAdapter

    private val githubRepos = mutableListOf<GithubRepo>()
    private val chatFiles = mutableListOf<ChatFile>()

    private var conversationId: String? = null

    private val filePickerLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent(),
    ) { uri: Uri? ->
        uri?.let { uploadFile(it) }
    }

    companion object {
        private const val PREFS_NAME = "chat_prefs"
        const val EXTRA_CONVERSATION_ID = "conversation_id"

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

        // Pre-fill GitHub username if saved
        val savedUsername = prefs.getString("github_username", "")
        if (!savedUsername.isNullOrEmpty()) {
            binding.etGithubUsername.setText(savedUsername)
        }

        setupRepoList()
        setupFileList()
        initializeVisibility()

        binding.btnClose.setOnClickListener {
            finish()
        }

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
            if (conversationId != null) {
                filePickerLauncher.launch("*/*")
            } else {
                Toast.makeText(this, "No active conversation", Toast.LENGTH_SHORT).show()
            }
        }

        // Load files for the current conversation
        loadChatFiles()
    }

    override fun onDestroy() {
        super.onDestroy()
        executor.shutdownNow()
    }

    override fun onResume() {
        super.onResume()
        // Reload files when returning to this activity
        if (conversationId != null) {
            loadChatFiles()
        }
    }

    private fun setupRepoList() {
        repoAdapter = GithubRepoAdapter()
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

                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", "http://10.0.2.2:8000") ?: "http://10.0.2.2:8000"

                if (apiKey.isEmpty()) {
                    runOnUiThread {
                        Toast.makeText(this, "API key not configured", Toast.LENGTH_LONG).show()
                        binding.btnLoadRepos.isEnabled = true
                    }
                    return@execute
                }

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
                                description = obj.optString("description", ""),
                                htmlUrl = obj.getString("html_url"),
                                defaultBranch = obj.optString("default_branch", "main"),
                                language = obj.optString("language", ""),
                                stars = obj.optInt("stargazers_count", 0),
                                isPrivate = obj.optBoolean("private", false),
                                selected = false
                            )
                        )
                    }

                    runOnUiThread {
                        githubRepos.clear()
                        githubRepos.addAll(repos)
                        repoAdapter.submitList(githubRepos)
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
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error loading repos", e)
                runOnUiThread {
                    val errorMsg = when {
                        e.message?.contains("failed to connect") == true -> 
                            "Cannot connect to backend. Please ensure the backend server is running."
                        e.message?.contains("timeout") == true -> 
                            "Connection timed out. Please check your backend server and network connection."
                        else -> "Error: ${e.message ?: "Unknown error"}"
                    }
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
                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", "http://10.0.2.2:8000") ?: "http://10.0.2.2:8000"

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
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error loading files", e)
                runOnUiThread {
                    binding.rvFiles.visibility = View.GONE
                    binding.tvNoFiles.visibility = View.VISIBLE
                    val errorMsg = when {
                        e.message?.contains("failed to connect") == true -> 
                            "Cannot connect to backend"
                        e.message?.contains("timeout") == true -> 
                            "Connection timed out"
                        else -> "Error: ${e.message ?: "Unknown error"}"
                    }
                    binding.tvNoFiles.text = errorMsg
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

                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", "http://10.0.2.2:8000") ?: "http://10.0.2.2:8000"

                // Use the chunked upload helper
                val success = ChatFileUploadHelper.uploadFile(
                    uri = uri,
                    conversationId = convId,
                    apiKey = apiKey,
                    baseUrl = baseUrl,
                    contentResolver = contentResolver,
                    cacheDir = cacheDir,
                    onProgress = { current, total ->
                        runOnUiThread {
                            if (total > 1) {
                                Toast.makeText(
                                    this,
                                    "Uploading… chunk $current/$total",
                                    Toast.LENGTH_SHORT
                                ).show()
                            }
                        }
                    }
                )

                runOnUiThread {
                    if (success) {
                        Toast.makeText(this, "File uploaded successfully", Toast.LENGTH_SHORT).show()
                        // Reload files to show the newly uploaded file
                        loadChatFiles()
                    } else {
                        Toast.makeText(this, "Failed to upload file", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                Log.e("ResourceActivity", "Error uploading file", e)
                runOnUiThread {
                    val errorMsg = when {
                        e.message?.contains("failed to connect") == true -> 
                            "Cannot connect to backend. Please ensure the backend server is running."
                        e.message?.contains("timeout") == true -> 
                            "Upload timed out. Please check your backend server and network connection."
                        else -> "Error uploading file: ${e.message}"
                    }
                    Toast.makeText(this, errorMsg, Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    private fun applySelections() {
        val convId = conversationId ?: run {
            finish()
            return
        }

        val selectedRepos = githubRepos.filter { it.selected }
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

                val apiKey = prefs.getString("api_key", "") ?: ""
                val baseUrl = prefs.getString("backend_url", "http://10.0.2.2:8000") ?: "http://10.0.2.2:8000"

                var successCount = 0
                var failureCount = 0

                // Add selected GitHub repos
                for (repo in selectedRepos) {
                    try {
                        val jsonBody = JSONObject().apply {
                            put("repo_url", repo.htmlUrl)
                            put("branch", repo.defaultBranch)
                        }.toString()

                        val request = Request.Builder()
                            .url("$baseUrl/api/chat/$convId/github/repos")
                            .addHeader("Authorization", "Bearer $apiKey")
                            .addHeader("Content-Type", "application/json")
                            .post(jsonBody.toRequestBody("application/json".toMediaType()))
                            .build()

                        val response = BackendClient.executeWithRetry(request)
                        if (response.isSuccessful) {
                            successCount++
                        } else {
                            failureCount++
                            Log.e("ResourceActivity", "Failed to add repo ${repo.fullName}: ${response.code}")
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
                        Toast.makeText(this, "Selections applied successfully", Toast.LENGTH_SHORT).show()
                        finish()
                    } else {
                        Toast.makeText(
                            this, 
                            "Applied with errors ($successCount succeeded, $failureCount failed)", 
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
}
