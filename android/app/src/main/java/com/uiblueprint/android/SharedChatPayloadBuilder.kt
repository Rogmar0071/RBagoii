package com.uiblueprint.android

import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException

private data class SharedRepoContext(
    val id: String,
)

private data class SharedFileContext(
    val id: String,
    val filename: String,
    val category: String,
    val mimeType: String,
    val includedInContext: Boolean,
)

object SharedChatPayloadBuilder {
    @Throws(IOException::class)
    fun build(
        message: String,
        conversationId: String,
        agentMode: Boolean,
        baseUrl: String,
        apiKey: String,
    ): String {
        val repos = fetchRepos(conversationId, baseUrl, apiKey)
        val files = fetchFiles(conversationId, baseUrl, apiKey)

        return JSONObject().apply {
            put("message", message)
            put("conversation_id", conversationId)
            put("agent_mode", agentMode)
            put(
                "context",
                JSONObject().apply {
                    put("session_id", JSONObject.NULL)
                    put("domain_profile_id", JSONObject.NULL)
                    if (repos.isNotEmpty()) {
                        put(
                            "repos",
                            JSONArray().apply {
                                repos.forEach { repo -> put(repo.id) }
                            },
                        )
                    }

                    val includedFiles = files.filter {
                        it.includedInContext && it.category != "github_repo"
                    }
                    if (includedFiles.isNotEmpty()) {
                        put(
                            "files",
                            JSONArray().apply {
                                includedFiles.forEach { file ->
                                    put(
                                        JSONObject().apply {
                                            put("id", file.id)
                                            put("filename", file.filename)
                                            put("category", file.category)
                                            put("mime_type", file.mimeType)
                                        },
                                    )
                                }
                            },
                        )
                    }
                },
            )
        }.toString()
    }

    @Throws(IOException::class)
    private fun fetchRepos(
        conversationId: String,
        baseUrl: String,
        apiKey: String,
    ): List<SharedRepoContext> {
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/api/chat/$conversationId/repos")
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .get()
            .build()

        BackendClient.executeWithRetry(request).use { response ->
            if (!response.isSuccessful) {
                throw IOException("Failed to load repo context: HTTP ${response.code}")
            }
            val repos = JSONArray(response.body?.string() ?: "[]")
            return buildList {
                for (index in 0 until repos.length()) {
                    add(SharedRepoContext(id = repos.getJSONObject(index).getString("id")))
                }
            }
        }
    }

    @Throws(IOException::class)
    private fun fetchFiles(
        conversationId: String,
        baseUrl: String,
        apiKey: String,
    ): List<SharedFileContext> {
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/api/chat/$conversationId/files")
            .apply { if (apiKey.isNotEmpty()) addHeader("Authorization", "Bearer $apiKey") }
            .get()
            .build()

        BackendClient.executeWithRetry(request).use { response ->
            if (!response.isSuccessful) {
                throw IOException("Failed to load file context: HTTP ${response.code}")
            }
            val files = JSONArray(response.body?.string() ?: "[]")
            return buildList {
                for (index in 0 until files.length()) {
                    val file = files.getJSONObject(index)
                    add(
                        SharedFileContext(
                            id = file.getString("id"),
                            filename = file.getString("filename"),
                            category = file.getString("category"),
                            mimeType = file.getString("mime_type"),
                            includedInContext = file.getBoolean("included_in_context"),
                        ),
                    )
                }
            }
        }
    }
}
