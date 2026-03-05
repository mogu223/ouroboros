        # 调用主系统的处理函数（通过 supervisor 引用）
        from supervisor.workers import handle_chat_direct
        import threading
        
        # 在后台线程中处理
        def process():
            try:
                # 使用 Discord 用户 ID 作为 chat_id (使用负数前缀区分 Telegram)
                # Telegram chat_id 是正数，Discord 使用负数
                chat_id = -1000000000000 - user_id
                handle_chat_direct(chat_id=chat_id, text=content, image_data=None)
            except Exception as e:
                logger.error(f"❌ Error in handle_chat_direct: {e}")
        
        thread = threading.Thread(target=process, daemon=True)
        thread.start()