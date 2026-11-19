from conan import ConanFile


# Replaces sqlcipher3 0.6.2's own conanfile.py for the #682 rebuild. setup.py
# runs `conan install` on it to get a static OpenSSL to link into the
# extension. Keep the version in step with OPENSSL_VERSION in pins.txt.
class OpensslRecipe(ConanFile):
    default_options = {
        # SQLCipher never initialises OpenSSL itself, so libcrypto's implicit
        # init would load openssl.cnf from OPENSSLDIR, and a config file can
        # load provider DLLs. The PyPI wheel's OPENSSLDIR is the build
        # machine's conan cache (C:\Users\runneradmin\.conan2\...). Nothing in
        # the app needs a config file: turn the autoload off, and point
        # OPENSSLDIR at OpenSSL's own admin-only Windows default.
        "openssl/*:no_autoload_config": True,
        "openssl/*:openssldir": "C:/Program Files/Common Files/SSL",
    }

    def requirements(self):
        self.requires('openssl/3.5.9')
