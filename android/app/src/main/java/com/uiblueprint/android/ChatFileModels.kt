package com.uiblueprint.android

import java.util.Date

/**
 * Data model for a file uploaded to a chat conversation.
 * Maps to the ChatFile backend model.
 */
data class ChatFile(
    val id: String,
    val conversationId: String,
    val filename: String,
    val mimeType: String,
    val sizeBytes: Long,
    val category: String,
    val includedInContext: Boolean,
    val createdAt: Date,
    val updatedAt: Date,
    val downloadUrl: String?,
)

/**
 * File category enum for grouping files in the UI.
 */
enum class FileCategory(val value: String) {
    DOCUMENT("document"),
    CODE("code"),
    IMAGE("image"),
    VIDEO("video"),
    AUDIO("audio"),
    DATA("data"),
    ARCHIVE("archive"),
    OTHER("other");

    companion object {
        fun fromString(value: String): FileCategory {
            return values().find { it.value == value } ?: OTHER
        }
    }
}

/**
 * Helper to format file size in human-readable format.
 */
fun formatFileSize(bytes: Long): String {
    val kb = bytes / 1024.0
    val mb = kb / 1024.0
    val gb = mb / 1024.0

    return when {
        gb >= 1.0 -> String.format("%.1f GB", gb)
        mb >= 1.0 -> String.format("%.1f MB", mb)
        kb >= 1.0 -> String.format("%.1f KB", kb)
        else -> "$bytes B"
    }
}
