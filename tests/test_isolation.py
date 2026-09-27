import main


def test_tests_never_touch_the_real_db(tmp_data_dir):
    # Regression: main.init_db() runs at import; without PFB_DATA_DIR the
    # suite would migrate and wipe the production data/feedback.db.
    assert main.DB_PATH.parent.resolve() == tmp_data_dir.resolve()
    assert "pfb-test-" in str(main.DB_PATH)
