# GitHub Bridge & Resource Management Setup

This document describes how the GitHub bridge and Resource management features are configured in RBagoii.

## Overview

The Resource management system allows users to:
1. Select GitHub repositories from their profile to include in AI context
2. Manage files uploaded to conversations (group by type, toggle inclusion in context)
3. Upload large files using automatic chunked uploads

## GitHub Bridge Setup

### 1. Backend Configuration

The GitHub bridge is configured via environment variables on the backend:

```bash
# Optional: GitHub Personal Access Token for higher rate limits and private repo access
GITHUB_TOKEN=ghp_your_token_here
```

**Without `GITHUB_TOKEN`:**
- Backend uses GitHub's public API (rate limit: 60 requests/hour per IP)
- Only public repositories are accessible

**With `GITHUB_TOKEN`:**
- Higher rate limit (5,000 requests/hour)
- Access to private repositories (if token has appropriate scopes)
- Repository listing includes private repos

### 2. Creating a GitHub Token

To enable full functionality:

1. Go to GitHub Settings → Developer settings → Personal access tokens
2. Click "Generate new token (classic)"
3. Select scopes:
   - `repo` - Full control of private repositories
   - `read:user` - Read user profile data
4. Generate and copy the token
5. Set `GITHUB_TOKEN` environment variable in your backend deployment (e.g., Render, Heroku)

### 3. Backend API Endpoints

The following endpoints are available:

#### Get Authenticated User
```
GET /api/github/user
Authorization: Bearer <API_KEY>
```

Returns the authenticated GitHub user's profile (requires `GITHUB_TOKEN`).

#### List User Repositories
```
GET /api/github/user/{username}/repos?page=1&per_page=30
Authorization: Bearer <API_KEY>
```

Lists all repositories for a GitHub user.

#### Add Repository to Conversation
```
POST /api/chat/{conversation_id}/github/repos
Authorization: Bearer <API_KEY>
Content-Type: application/json

{
  "repo_url": "https://github.com/owner/repo",
  "branch": "main"
}
```

#### List Repositories in Conversation
```
GET /api/chat/{conversation_id}/github/repos
Authorization: Bearer <API_KEY>
```

#### Remove Repository from Conversation
```
DELETE /api/chat/{conversation_id}/github/repos/{repo_id}
Authorization: Bearer <API_KEY>
```

## Resource Screen (Android)

The Resource screen is accessible via the search icon in the chat toolbar (top-right corner).

### Features

1. **GitHub Repositories Section**
   - Enter a GitHub username
   - Click "Load Repositories" to fetch repos
   - Select repositories with checkboxes
   - View repo metadata: name, description, language, stars

2. **Files Section**
   - View all files uploaded to the conversation
   - Files grouped by category: document, code, image, video, audio, data, archive, other
   - Toggle checkboxes to include/exclude files from AI context

3. **Apply Button**
   - Saves selected repositories and file settings
   - Updates backend via API calls
   - Returns to chat screen

### Usage in Normal and Strict Mode

The Resource selections apply to both normal and strict mode:
- Selected repositories are included in AI context
- Files marked "included_in_context" are available to AI
- Mode toggle only affects response format, not resource availability

## File Upload with Chunking

### How It Works

Files are automatically chunked when:
- File size > 5 MB AND
- File is NOT an image (images are always uploaded as single files)

### Chunking Process

1. **Client Side (Android):**
   - File is split into 5 MB chunks
   - Each chunk uploaded via `POST /api/chat/{conversation_id}/files/chunks`
   - Headers include: `X-Upload-Id`, `X-Chunk-Index`, `X-Total-Chunks`, `X-Filename`
   - Progress shown to user: "Uploading… chunk N/M"

2. **Server Side (Backend):**
   - Chunks stored temporarily in `/tmp/ui_blueprint_data/chunk_uploads/`
   - Manifest tracks received chunks
   - After all chunks received, client calls finalize endpoint

3. **Finalization:**
   - `PUT /api/chat/{conversation_id}/files/chunks/{upload_id}/finalize`
   - Server assembles chunks into complete file
   - Extracts text content (for code/text files)
   - Stores in object storage (S3/local)
   - Creates ChatFile database record
   - Chunks deleted after successful assembly

### Benefits

- Reliable uploads for large files
- Automatic retry on failed chunks
- Progress tracking
- Reduced memory usage
- Works with flaky network connections

## Database Schema

### ChatFile Table

Stores all uploaded files and GitHub repos (repos stored as special category).

```sql
CREATE TABLE chat_files (
  id UUID PRIMARY KEY,
  conversation_id VARCHAR NOT NULL,
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  category VARCHAR NOT NULL,  -- document, code, image, video, audio, data, archive, github_repo
  included_in_context BOOLEAN DEFAULT TRUE,
  extracted_text TEXT,  -- AI-friendly text content
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);
```

GitHub repositories are stored with:
- `category = 'github_repo'`
- `mime_type = 'application/x-git-repository'`
- `object_key = 'github:{url}@{branch}'`
- `extracted_text = 'GitHub Repository: {owner}/{repo}...'`

## Testing

### Backend Tests

```bash
cd backend
pytest tests/test_github_routes.py -v
pytest tests/test_chat_file_routes.py -v
```

### Android Tests

```bash
cd android
./gradlew :app:testDebugUnitTest
```

### Manual Testing

1. **GitHub Bridge:**
   - Set `GITHUB_TOKEN` in backend
   - Open Resource screen in Android app
   - Enter your GitHub username
   - Verify repos load correctly
   - Select repos and apply
   - Verify repos appear in file list with checkboxes

2. **File Upload:**
   - Upload small file (<5 MB) - should use single upload
   - Upload large file (>5 MB) - should use chunked upload
   - Monitor logs for chunk progress
   - Verify file appears in conversation

3. **Resource Screen:**
   - Open Resource screen from chat
   - Load GitHub repos
   - Select/deselect repos
   - Toggle file checkboxes
   - Click Apply
   - Verify changes persist

## Troubleshooting

### GitHub API Rate Limit Exceeded

**Symptom:** "Failed to load GitHub repositories" after 60 requests/hour

**Solution:** Configure `GITHUB_TOKEN` environment variable on backend

### Chunked Upload Fails

**Symptom:** Large file upload fails partway through

**Solution:**
- Check backend logs for specific chunk failure
- Verify `DATA_DIR` has sufficient disk space
- Increase `MAX_UPLOAD_BYTES` if file exceeds 50 MB default

### Resources Not Appearing in AI Context

**Symptom:** Selected repos/files not used by AI

**Solution:**
- Verify `included_in_context = true` in database
- Check that conversation_id matches
- Ensure backend includes ChatFile data when building AI prompts

## Future Enhancements

Potential improvements:
1. GitHub OAuth integration (replace manual username entry)
2. Repo file browsing (select specific files from repos)
3. Webhook integration (auto-update repos on push)
4. Private repo caching (avoid repeated API calls)
5. Resume interrupted chunked uploads
6. Parallel chunk uploads (faster for large files)
