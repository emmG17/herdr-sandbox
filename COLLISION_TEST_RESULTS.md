# Herdr Pane ID Collision Test Results (Herdr 0.9.5)

## Test 1: Workspace ID Reuse After Closure

### Commands
```bash
# Start server
herdr server &
sleep 3

# Create first workspace
herdr workspace create --label "test1"
# Result: workspace_id=w1, pane_id=w1:p1

# Close workspace
herdr workspace close "w1"

# Create second workspace
herdr workspace create --label "test2"
# Result: workspace_id=w2, pane_id=w2:p1
```

### Result
✅ **NO COLLISION**: Workspace IDs are NOT reused after closure
- First workspace: w1, first pane: w1:p1
- Second workspace: w2, second pane: w2:p1

## Test 2: Multiple Servers with Different Sockets

### Commands
```bash
# Start two separate servers
HERDR_SOCKET_PATH="/tmp/herdr-a.sock" herdr server &
HERDR_SOCKET_PATH="/tmp/herdr-b.sock" herdr server &
sleep 3

# Create workspace in server A
HERDR_SOCKET_PATH="/tmp/herdr-a.sock" herdr workspace create --label "session-a"
# Result: workspace_id=w1, pane_id=w1:p1

# Create workspace in server B
HERDR_SOCKET_PATH="/tmp/herdr-b.sock" herdr workspace create --label "session-b"
# Result: workspace_id=w1, pane_id=w1:p1

# Check pane environments
HERDR_SOCKET_PATH="/tmp/herdr-a.sock" herdr pane run "w1:p1" \
  'echo "SOCKET=$HERDR_SOCKET_PATH PANE=$HERDR_PANE_ID WS=$HERDR_WORKSPACE_ID"'
# Output: SOCKET=/tmp/herdr-a.sock PANE=w1:p1 WS=w1

HERDR_SOCKET_PATH="/tmp/herdr-b.sock" herdr pane run "w1:p1" \
  'echo "SOCKET=$HERDR_SOCKET_PATH PANE=$HERDR_PANE_ID WS=$HERDR_WORKSPACE_ID"'
# Output: SOCKET=/tmp/herdr-b.sock PANE=w1:p1 WS=w1
```

### Result
❌ **COLLISION DETECTED**: Multiple Herdr servers CAN have the same pane ID simultaneously!

- Server A: workspace=w1, pane=w1:p1, socket=/tmp/herdr-a.sock
- Server B: workspace=w1, pane=w1:p1, socket=/tmp/herdr-b.sock

## Discriminator Available

✅ **HERDR_SOCKET_PATH is available** in pane environments and differs per server:
- Server A pane sees: `/tmp/herdr-a.sock`
- Server B pane sees: `/tmp/herdr-b.sock`

## Conclusion

**Reuse key must include `HERDR_SOCKET_PATH`** to prevent collision across multiple Herdr servers.

### Updated Reuse Key

**From**: `(pane_id, source_repo, agent)`  
**To**: `(pane_id, socket_path, source_repo, agent)`

Where `socket_path` comes from `HERDR_SOCKET_PATH` environment variable.

### Edge Cases

1. **Missing HERDR_PANE_ID**: Always create new worker (unchanged)
2. **Missing HERDR_SOCKET_PATH**: Treat as separate session, record as None
3. **Same pane ID, different socket**: Different workers (prevents collision)
4. **Workspace closure**: Not an issue (workspace IDs don't reuse)
