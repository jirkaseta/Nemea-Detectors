/*!
 * \file config.h
 * \brief Interface of the Configuration singleton
 * \author Tomas Cejka <cejkat@cesnet.cz>
 * \date 2013
 * \date 2012
 */
/*
 * Copyright (C) 2013 CESNET
 *
 * LICENSE TERMS
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in
 *    the documentation and/or other materials provided with the
 *    distribution.
 * 3. Neither the name of the Company nor the names of its contributors
 *    may be used to endorse or promote products derived from this
 *    software without specific prior written permission.
 *
 * ALTERNATIVELY, provided that this notice is retained in full, this
 * product may be distributed under the terms of the GNU General Public
 * License (GPL) version 2 or later, in which case the provisions
 * of the GPL apply INSTEAD OF those given above.
 *
 * This software is provided ``as is'', and any express or implied
 * warranties, including, but not limited to, the implied warranties of
 * merchantability and fitness for a particular purpose are disclaimed.
 * In no event shall the company or contributors be liable for any
 * direct, indirect, incidental, special, exemplary, or consequential
 * damages (including, but not limited to, procurement of substitute
 * goods or services; loss of use, data, or profits; or business
 * interruption) however caused and on any theory of liability, whether
 * in contract, strict liability, or tort (including negligence or
 * otherwise) arising in any way out of the use of this software, even
 * if advised of the possibility of such damage.
 *
 */
#ifndef _HS_CONFIG_H
#define _HS_CONFIG_H

#include <iostream>
#include <fstream>
#include <map>
#include <string>
#include <pthread.h>

#define INI_DELIM '='
#define INI_DEFAULT_FILENAME "hoststats.conf.default"
#define INI_FILENAME "hoststats.conf"
#define INI_WHITESPACE " \n\r\t"

using namespace std;

class Configuration {
private:
   map<string, string> values;   
   static pthread_mutex_t config_mutex;
   void parseLine(string line);
   Configuration();
   Configuration(Configuration const&){};
   Configuration& operator=(Configuration const&);
   ~Configuration(){};
   static Configuration *instance;
   void trimString(string &text);
   void load();
   void clean();
public:
   string getValue(string paramName);
   friend ostream &operator<<(ostream &i, Configuration &c)
   {
      map<string, string>::iterator it;
      i << "Configuration:" << endl;
      for (it=c.values.begin(); it!=c.values.end(); ++it) {
         i << "\"" << (*it).first << "\"" << " -> " << "\"" << (*it).second << "\"" << endl;
      }
      return i;
   }

   /**
    * \brief Force Configuration to reread configuration file
    */
   void reload();

   /**
    * \brief Lock current setting to prevent reloading during multiple getValue()
    * \return 0 on success, otherwise nonzero
    */
   int lock();

   /**
    * \brief Unlock current setting - opposite to lock()
    * \return 0 on success, otherwise nonzero
    */
   int unlock();

   static Configuration *getInstance();

   /**
    * \brief Clean-up on application exit.
    * Warning: Do not use this class after calling this method!
    */
   static void freeConfiguration();
};

#endif
