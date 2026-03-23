import sqlite3
import pickle
from collections import namedtuple
import uuid

from session_samples import Sample
from session_samples import SessionSamples

#Sample is namedtuple

class SamplesDBase():
    def __init__(self, db_file, uid):
        self.db_file = db_file
        self.uid = uid
        self._create_connect()
        pass
    def __del__(self):
        self._close()
    def _create_connect(self):
        self._close()
        self._create_table()
        pass
    def _create_table(self):
        pass
    def insert(self, session_id, samples):
        pass
    def retrieve(self, session_id):#->list of Sample
        pass
    def view_content(self):
        pass
    def _close(self):
        if self.conn:
            self.conn.close()
        pass
""" """
class SamplesDBsqlite(SamplesDBase):
    def __init__(self, db_file, uid):
        super().__init__(db_file, uid)
        pass
    """Override"""
    def _create_connect(self):
        """Create a database connection to the SQLite database."""
        self.conn = None
        # Setup GUID Adapter/Converter for SQLite
        # Store GUID as 16 bytes, retrieve as UUID object
        sqlite3.register_adapter(uuid.UUID, lambda u: u.bytes_le)
        sqlite3.register_converter("GUID", lambda b: uuid.UUID(bytes_le=b))
        self.conn = sqlite3.connect(self.db_file, detect_types=sqlite3.PARSE_DECLTYPES)
        self._create_table()
        pass
    """Override"""
    def _create_table(self):
        """Create a table to store records with a related ID."""
        self.cursor = self.conn.cursor()
        # The 'data' column has a BLOB data type
        self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS samples (
                    session_id INTEGER PRIMARY KEY,
                    id GUID  NOT NULL,
                    data BLOB NOT NULL
                );
        ''')
        self.conn.commit()
        pass
    """Override"""
    def insert(self, session_id, samples_list):
        """Serialize and insert a list of named tuples into the database."""
        try:
            # Serialize the list of named tuples to a byte string (BLOB)
            serialized_data = pickle.dumps(samples_list)
            #self.cursor = conn.cursor()
            self.cursor.execute('''
                INSERT OR REPLACE INTO samples (session_id, id, data) VALUES (?, ?, ?)
            ''', (session_id, self.uid, serialized_data))
            self.conn.commit()
            print(f"Inserted samples for session_id {session_id}")
        except sqlite3.Error as e:
            print(e)
        pass
    """Override"""
    def retrieve(self, session_id):
        """Retrieve and deserialize a list of named tuples from the database."""
        try:
            #self.cursor = conn.cursor()
            self.cursor.execute('''
                SELECT data FROM samples WHERE session_id = ? AND id = ?
            ''', (session_id, self.uid))
            row = self.cursor.fetchone()
            if row:
                # Deserialize the byte string back to a list of named tuples
                deserialized_data = pickle.loads(row[0])
                return deserialized_data
            else:
                return None
        except sqlite3.Error as e:
            print(e)
        pass
    """ """
    def view_content(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_file)
            cur = conn.cursor()
            # Find all table names
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cur.fetchall()   
            print(f"Found tables: {tables}\n")
            for table_name_tuple in tables:
                table_name = table_name_tuple[0]
                print(f"--- Contents of table: {table_name} ---")
                # Select all rows from the table
                cur.execute(f"SELECT * FROM {table_name}")
                rows = cur.fetchall()
                # Print column headers (optional)
                headers = [description[0] for description in cur.description]
                print(headers)
                # Print the data rows
                if False:
                    for row in rows:
                        print(row)
                    print("\n")
        except sqlite3.Error as e:
            print(f"An error occurred: {e}")
        finally:
            if conn:
                conn.close()
        pass
# --- Example Usage ---
if __name__ == '__main__':
    database = "bf_sessions_db.sqlite"
    try:
        # Create a unique ID
        uid = uuid.uuid4()
        sdb = SamplesDBsqlite(database, uid)
        sdb.view_content()
        #class Sample(NamedTuple):
        #side: np.uint32
        #cam_id: np.uint32
        #frame_id: np.uint32
        #timestamp: np.float32
        #hist: npt.NDArray[np.uint32]
        #temp: np.float32
        #summ:  np.uint64

        samples_to_store = [
            Sample(side=1,cam_id=0,frame_id=1,timestamp=0.1,hist=[1,2,3],temp=0.0,summ=1),
            Sample(side=1,cam_id=0,frame_id=1,timestamp=0.1,hist=[1,2,3],temp=0.0,summ=1),
            Sample(side=1,cam_id=0,frame_id=1,timestamp=0.1,hist=[1,2,3],temp=0.0,summ=1)
        ]
        
        session_id = 1
        
        # Store
        sdb.insert(session_id, samples_to_store)
        # Retrieve
        retrieved_samples = sdb.retrieve(session_id)
        # Verify
        if retrieved_samples:
            print(f"\nRetrieved samples for session_id {session_id}:")
            for sample in retrieved_samples:
                print(f"  Name: {sample.side}, Value: {sample.summ}, Timestamp: {sample.timestamp}")
            print(f"\nType of retrieved data: {type(retrieved_samples)}")
            print(f"Type of an element: {type(retrieved_samples[0])}")
        else:
            print(f"\nNo samples found for session_id {session_id}")

        # Close the connection
        #conn.close()
    except Exception as e:
        pass